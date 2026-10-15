use std::path::PathBuf;

use anyhow::Result;
use clap::Args;
use plume_core::auth::probe::probe_gsa_init;

/// The fixed user agent of tvsign 1.0.0 (macOS, 2018).
const LEGACY_USER_AGENT: &str = "akd/1.0 CFNetwork/978.0.7 Darwin/18.7.0";

#[derive(Debug, Args)]
pub struct ProbeArgs {
    /// Anisette v3 server to use as it is (no replacement of Android servers, no fallback)
    #[arg(long, value_name = "URL", default_value = plume_core::DEFAULT_ANISETTE_URL_V3)]
    pub server: String,
    /// Directory of the anisette identity of the probe (default: a fresh temporary directory)
    #[arg(long, value_name = "DIR")]
    pub state_dir: Option<PathBuf>,
    /// Send the fixed macOS user agent of tvsign 1.0.0 instead of the one the server reports
    #[arg(long)]
    pub legacy_user_agent: bool,
}

pub async fn execute(args: ProbeArgs) -> Result<()> {
    let state_dir = args.state_dir.unwrap_or_else(|| {
        std::env::temp_dir().join(format!("plumesign-probe-{:016x}", std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos() as u64).unwrap_or(0)))
    });
    let report = probe_gsa_init(
        &args.server,
        state_dir.clone(),
        args.legacy_user_agent.then_some(LEGACY_USER_AGENT),
    )
    .await?;
    println!("server            : {}", report.server);
    println!("state dir         : {}", state_dir.display());
    println!("X-Mme-Client-Info : {}", report.client_info);
    println!(
        "User-Agent        : {}{}",
        report.user_agent,
        if report.user_agent_overridden { "  (legacy 1.0.0, not the server's)" } else { "" }
    );
    println!("identity is new   : {}", report.identity_is_new);
    for (k, v) in &report.headers {
        println!("  header {k}: {v}");
    }
    for a in &report.attempts {
        println!("--- {} ---", a.label);
        println!("HTTP status       : {}", a.http_status);
        println!("ec                : {:?}", a.ec);
        println!("em                : {:?}", a.em);
        println!("hsc               : {:?}", a.hsc);
        println!("salt+B returned   : {}", a.has_salt_and_b);
        println!("asks resync (434) : {}", a.asks_resync);
        if let Some(body) = &a.body_snippet {
            println!("non-plist body    : {body}");
        }
        println!("ACCEPTED          : {}", a.accepted());
    }
    Ok(())
}
