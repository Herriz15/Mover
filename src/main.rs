use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::thread::sleep;
use std::time::{Duration, Instant};

use anyhow::{bail, Context, Result};
use clap::Parser;
use serde_json::Value;
use ureq::Agent;
use ureq::AgentBuilder;

#[derive(Parser, Debug)]
#[command(author, version, about = "Bridge Codex to a local Ollama runtime", long_about = None)]
struct Cli {
    /// Path to the Codex executable (defaults to `codex` on $PATH)
    #[arg(long, default_value = "codex")]
    codex_bin: PathBuf,

    /// Additional arguments passed to the Codex executable
    #[arg(trailing_var_arg = true)]
    codex_args: Vec<String>,

    /// Path to the Ollama executable (defaults to `ollama` on $PATH)
    #[arg(long, default_value = "ollama")]
    ollama_bin: String,

    /// Ollama model to pull and warm
    #[arg(long, default_value = "llama3.2:3b")]
    model: String,

    /// Host interface for the Ollama server
    #[arg(long, default_value = "127.0.0.1")]
    host: String,

    /// Port for the Ollama server
    #[arg(long, default_value_t = 11434)]
    port: u16,

    /// API key exposed to Codex via OPENAI_API_KEY
    #[arg(long, default_value = "ollama")]
    api_key: String,

    /// Seconds to wait for `ollama serve` to become available
    #[arg(long, default_value_t = 45)]
    readiness_timeout: u64,

    /// Skip pulling the model before warm-up
    #[arg(long)]
    skip_pull: bool,

    /// Skip issuing a warm-up request once the server is ready
    #[arg(long)]
    no_warmup: bool,

    /// Only ensure `ollama serve` is running without launching Codex
    #[arg(long)]
    serve_only: bool,

    /// Prompt used when warming the Ollama model
    #[arg(long, default_value = "Codex warm-up ping.")]
    warm_prompt: String,
}

struct OllamaSupervisor {
    host: String,
    port: u16,
    started_here: bool,
    child: Option<Child>,
}

impl OllamaSupervisor {
    fn ensure_running(cli: &Cli) -> Result<Self> {
        let mut supervisor = Self {
            host: cli.host.clone(),
            port: cli.port,
            started_here: false,
            child: None,
        };

        if supervisor.is_reachable() {
            println!(
                "[mover] detected existing Ollama server on {}:{}",
                supervisor.host, supervisor.port
            );
            return Ok(supervisor);
        }

        println!(
            "[mover] starting `{} serve` bound to {}:{}",
            cli.ollama_bin, cli.host, cli.port
        );
        let mut cmd = Command::new(&cli.ollama_bin);
        cmd.arg("serve")
            .env("OLLAMA_HOST", &cli.host)
            .env("OLLAMA_PORT", cli.port.to_string())
            .stdin(Stdio::null())
            .stdout(Stdio::inherit())
            .stderr(Stdio::inherit());
        let child = cmd.spawn().context("failed to spawn `ollama serve`")?;
        supervisor.child = Some(child);
        supervisor.started_here = true;
        supervisor.wait_until_ready(Duration::from_secs(cli.readiness_timeout))?;
        Ok(supervisor)
    }

    fn wait_until_ready(&mut self, timeout: Duration) -> Result<()> {
        let start = Instant::now();
        loop {
            if self.is_reachable() {
                return Ok(());
            }

            if let Some(child) = self.child.as_mut() {
                if let Some(status) = child.try_wait()? {
                    bail!(
                        "`ollama serve` exited prematurely with status {}",
                        format_exit_status(status)
                    );
                }
            }

            if start.elapsed() > timeout {
                bail!(
                    "timed out waiting for `ollama serve` to become ready on {}:{}",
                    self.host,
                    self.port
                );
            }

            sleep(Duration::from_millis(250));
        }
    }

    fn is_reachable(&self) -> bool {
        let url = format!("http://{}:{}/api/tags", self.host, self.port);
        let agent = build_agent(Duration::from_secs(2), Duration::from_secs(2));
        match agent.get(&url).call() {
            Ok(resp) => resp.status() < 500,
            Err(_) => false,
        }
    }
}

impl Drop for OllamaSupervisor {
    fn drop(&mut self) {
        if self.started_here {
            if let Some(child) = self.child.as_mut() {
                if let Err(err) = child.kill() {
                    eprintln!("[mover] failed to terminate `ollama serve`: {err}");
                }
                let _ = child.wait();
            }
        }
    }
}

fn ensure_model_available(cli: &Cli) -> Result<()> {
    if cli.skip_pull {
        return Ok(());
    }

    println!("[mover] pulling model {}", cli.model);
    let status = Command::new(&cli.ollama_bin)
        .arg("pull")
        .arg(&cli.model)
        .status()
        .context("failed to run `ollama pull`")?;
    if !status.success() {
        bail!(
            "`ollama pull` exited with status {}",
            format_exit_status(status)
        );
    }
    Ok(())
}

fn warm_model(cli: &Cli) -> Result<()> {
    if cli.no_warmup {
        return Ok(());
    }

    println!("[mover] warming model {} with a short prompt", cli.model);
    let url = format!("http://{}:{}/api/generate", cli.host, cli.port);
    let body = serde_json::json!({
        "model": cli.model,
        "prompt": cli.warm_prompt,
        "stream": false,
        "options": {
            "temperature": 0.0,
            "num_predict": 16,
        }
    });
    let agent = build_agent(Duration::from_secs(5), Duration::from_secs(30));
    let response = agent.post(&url).send_json(body);

    match response {
        Ok(resp) => {
            if resp.status() >= 400 {
                bail!("warm-up request failed with HTTP {}", resp.status());
            }
            let value: Value = resp
                .into_json()
                .context("failed to decode warm-up response from Ollama")?;
            if let Some(error) = value.get("error") {
                bail!("Ollama warm-up error: {error}");
            }
            Ok(())
        }
        Err(ureq::Error::Status(code, resp)) => {
            let text = resp.into_string().unwrap_or_default();
            bail!("warm-up request failed with HTTP {code}: {text}");
        }
        Err(err) => bail!("failed to send warm-up request to Ollama: {err}"),
    }
}

fn run_codex(cli: &Cli) -> Result<ExitStatus> {
    let base_url = format!("http://{}:{}/v1", cli.host, cli.port);
    let codex_path = resolve_codex_bin(&cli.codex_bin)?;
    println!("[mover] launching Codex via `{}`", codex_path.display());
    let mut command = Command::new(&codex_path);
    command
        .args(&cli.codex_args)
        .env("OPENAI_API_BASE", &base_url)
        .env("OPENAI_API_KEY", &cli.api_key)
        .env("OLLAMA_HOST", &cli.host)
        .env("OLLAMA_PORT", cli.port.to_string())
        .stdin(Stdio::inherit())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit());
    let status = command.status().context("failed to launch Codex")?;
    Ok(status)
}

fn resolve_codex_bin(path: &Path) -> Result<PathBuf> {
    if path.as_os_str().is_empty() {
        bail!("`--codex-bin` cannot be empty");
    }

    if path.is_dir() {
        let candidates = ["codex", "codex.exe", "codex.cmd", "codex.ps1", "codex.bat"];

        for candidate in candidates {
            let candidate_path = path.join(candidate);
            if candidate_path.is_file() {
                return Ok(candidate_path);
            }
        }

        bail!(
            "`--codex-bin` points at directory `{}` but no Codex executable was found inside. Specify the executable directly or place it next to one of: {:?}.",
            path.display(),
            candidates
        );
    }

    if path.is_file() {
        return Ok(path.to_path_buf());
    }

    match which::which(path) {
        Ok(resolved) => Ok(resolved),
        Err(_) => bail!(
            "failed to locate Codex executable at `{}` or on PATH",
            path.display()
        ),
    }
}

fn format_exit_status(status: ExitStatus) -> String {
    match status.code() {
        Some(code) => code.to_string(),
        None => String::from("signal"),
    }
}

fn build_agent(connect: Duration, read: Duration) -> Agent {
    AgentBuilder::new()
        .timeout_connect(connect)
        .timeout_read(read)
        .timeout(connect + read)
        .build()
}

fn main() -> Result<()> {
    let cli = Cli::parse();

    let supervisor = OllamaSupervisor::ensure_running(&cli)?;
    ensure_model_available(&cli)?;
    warm_model(&cli)?;

    if cli.serve_only {
        println!("[mover] ollama is ready on {}:{}", cli.host, cli.port);
        // Prevent the supervisor from being dropped immediately so the child keeps running.
        std::mem::forget(supervisor);
        return Ok(());
    }

    let status = run_codex(&cli)?;
    if !status.success() {
        bail!("Codex exited with status {}", format_exit_status(status));
    }
    Ok(())
}
