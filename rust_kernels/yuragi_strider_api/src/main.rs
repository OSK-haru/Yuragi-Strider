use std::env;
use std::env::VarError;
use std::error::Error;

use yuragi_strider_api::{api_router, parse_allowed_origins};

#[tokio::main]
async fn main() {
    if let Err(error) = run().await {
        eprintln!("yuragi-strider-api startup/runtime error: {error}");
        std::process::exit(1);
    }
}

async fn run() -> Result<(), Box<dyn Error>> {
    let profile = environment_value("RUST_API_PROFILE", "production")?;
    let require_https_origins = match profile.as_str() {
        "development" => false,
        "staging" | "production" => true,
        _ => return Err(format!("unsupported RUST_API_PROFILE: {profile}").into()),
    };
    let raw_origins = environment_value("ALLOWED_ORIGINS", "")?;
    let allowed_origins = parse_allowed_origins(&raw_origins, require_https_origins)?;
    let host = environment_value("BIND_HOST", "0.0.0.0")?;
    if host.is_empty() || host.chars().any(char::is_control) {
        return Err("BIND_HOST must be a non-empty host name or IP address".into());
    }
    let raw_port = environment_value("PORT", "8000")?;
    let port = raw_port
        .parse::<u16>()
        .ok()
        .filter(|value| *value != 0)
        .ok_or_else(|| format!("PORT must be an integer from 1 to 65535: {raw_port}"))?;

    let address = if host.contains(':') {
        format!("[{host}]:{port}")
    } else {
        format!("{host}:{port}")
    };
    let listener = tokio::net::TcpListener::bind(&address).await?;
    eprintln!(
        "yuragi-strider-api listening on {address} profile={profile} allowed_origins={}",
        allowed_origins.len()
    );
    axum::serve(listener, api_router(allowed_origins))
        .with_graceful_shutdown(shutdown_signal())
        .await?;
    Ok(())
}

fn environment_value(name: &str, default: &str) -> Result<String, String> {
    match env::var(name) {
        Ok(value) => Ok(value),
        Err(VarError::NotPresent) => Ok(default.to_owned()),
        Err(VarError::NotUnicode(_)) => Err(format!("{name} must contain valid UTF-8")),
    }
}

async fn shutdown_signal() {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{SignalKind, signal};
        let mut terminate =
            signal(SignalKind::terminate()).expect("failed to register SIGTERM handler");
        tokio::select! {
            _ = tokio::signal::ctrl_c() => {},
            _ = terminate.recv() => {},
        }
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}
