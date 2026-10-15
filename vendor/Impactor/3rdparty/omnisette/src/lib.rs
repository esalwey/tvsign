//! A library to generate "anisette" data. Docs are coming soon.
//!
//! If you want an async API, enable the `async` feature.
//!
//! If you want remote anisette, make sure the `remote-anisette` feature is enabled. (it's currently on by default)

use crate::adi_proxy::{ADIProxyAnisetteProvider, ConfigurableADIProxy};
use crate::anisette_headers_provider::AnisetteHeadersProvider;
use adi_proxy::ADIError;
use std::io;
use std::path::PathBuf;
use thiserror::Error;

pub mod adi_proxy;
pub mod anisette_headers_provider;
pub mod store_services_core;

#[cfg(feature = "remote-anisette-v3")]
pub mod remote_anisette_v3;

#[allow(dead_code)]
pub struct AnisetteHeaders;

#[allow(dead_code)]
#[derive(Debug, Error)]
pub enum AnisetteError {
    #[allow(dead_code)]
    #[error("Unsupported device")]
    UnsupportedDevice,
    #[error("Invalid argument {0}")]
    InvalidArgument(String),
    #[error("Anisette not provisioned!")]
    AnisetteNotProvisioned,
    #[error("Plist serialization error {0}")]
    PlistError(#[from] plist::Error),
    #[error("Request Error {0}")]
    ReqwestError(#[from] reqwest::Error),
    #[cfg(feature = "remote-anisette-v3")]
    #[error("Provisioning socket error {0}")]
    WsError(#[from] tokio_tungstenite::tungstenite::error::Error),
    #[cfg(feature = "remote-anisette-v3")]
    #[error("JSON error {0}")]
    SerdeError(#[from] serde_json::Error),
    #[error("IO error {0}")]
    IOError(#[from] io::Error),
    #[error("ADI error {0}")]
    ADIError(#[from] ADIError),
    #[error("Invalid library format")]
    InvalidLibraryFormat,
    #[error("Misc")]
    Misc,
    #[error("Missing Libraries")]
    MissingLibraries,
    #[error("{0}")]
    Anyhow(#[from] anyhow::Error),
    /// The anisette server answered, but not with usable headers.
    #[error("anisette server error: {0}")]
    ServerError(String),
    /// Provisioning the device identity failed (websocket protocol, server timeout, closed socket).
    #[error("anisette provisioning failed: {0}")]
    ProvisioningFailed(String),
    /// Apple's provisioning service (gsa.apple.com) answered HTTP `status` without a usable plist.
    #[error("Apple answered HTTP {status} to the anisette provisioning: {message}")]
    AppleRefused { status: u16, message: String },
    /// The server announces a platform that Apple refuses (anything but Windows since 2026-10-12):
    /// nothing is sent to Apple with its data.
    #[error("anisette server not usable: {0}")]
    UnsupportedServer(String),
    /// The provider cannot synchronize with a SIM (hsc 434).
    #[error("anisette resync is not supported by this provider")]
    ResyncUnsupported,
    /// Every candidate server failed (default server and its fallback).
    #[error("no anisette server could be used: {0}")]
    AllServersFailed(String),
}

pub const DEFAULT_ANISETTE_URL: &str = "https://ani.f1sh.me/";

/// Anisette v3 server that serves Windows (iTunes) anisette. Since 2026-10-12 Apple's GrandSlam
/// refuses the data of the servers that generate Android anisette (error -80045, then HTTP 503).
pub const DEFAULT_ANISETTE_URL_V3: &str = "https://ani.jkcoxson.com";

/// Windows anisette server used when [`DEFAULT_ANISETTE_URL_V3`] cannot be reached.
pub const FALLBACK_ANISETTE_URL_V3: &str = "https://ani2.stikstore.app";

/// Anisette v3 servers that generate Android anisette: Apple refuses what they produce.
/// A configured URL on one of these hosts is replaced by the default server.
pub const ANDROID_ANISETTE_HOSTS: &[&str] = &[
    "ani.sidestore.app",
    "ani.stikstore.app",
    "anisette.crystall1ne.dev",
    "ani3server.fly.dev",
    "ani.jaydenha.uk",
    "omni.ethxn.xyz",
];

/// First non-empty URL among the given environment variables, in order.
fn env_url(names: &[&str]) -> Option<String> {
    names.iter().find_map(|name| {
        std::env::var(name)
            .ok()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
    })
}

/// Lower-case host of an http(s) URL (no scheme, credentials, port, path); `None` when empty.
/// A bare `host[:port]` without scheme is accepted.
pub fn url_host(url: &str) -> Option<String> {
    let url = url.trim();
    let rest = url.split_once("://").map_or(url, |(_, rest)| rest);
    let authority = rest.split(['/', '?', '#']).next().unwrap_or("");
    let authority = authority.rsplit('@').next().unwrap_or("");
    let host = if authority.starts_with('[') {
        // [::1]:8080
        authority.split(']').next().map(|h| format!("{h}]"))?
    } else {
        authority.split(':').next().unwrap_or("").to_string()
    };
    let host = host.trim_end_matches('.').to_ascii_lowercase();
    (!host.is_empty()).then_some(host)
}

/// True when `url` points at one of the [`ANDROID_ANISETTE_HOSTS`].
pub fn is_android_anisette_server(url: &str) -> bool {
    url_host(url).is_some_and(|host| ANDROID_ANISETTE_HOSTS.contains(&host.as_str()))
}

fn same_server(a: &str, b: &str) -> bool {
    a.trim().trim_end_matches('/').eq_ignore_ascii_case(b.trim().trim_end_matches('/'))
}

/// The anisette v3 servers to use, in order, and what to tell the user about the choice.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AnisetteServers {
    pub primary: String,
    pub fallback: Option<String>,
    pub warnings: Vec<String>,
}

/// Decide which servers to use.
///
/// - nothing requested: the default server, with the fallback server behind it;
/// - a server that generates Android anisette: replaced by the default (Apple refuses its data);
/// - the default server asked explicitly: same as nothing requested;
/// - any other server: used alone (a private server is never silently swapped for a public one);
/// - `fallback_override`: replaces the fallback (`none` or `off` removes it).
pub fn resolve_servers(requested: Option<&str>, fallback_override: Option<&str>) -> AnisetteServers {
    let mut warnings = Vec::new();
    let mut primary = DEFAULT_ANISETTE_URL_V3.to_string();
    let mut fallback = Some(FALLBACK_ANISETTE_URL_V3.to_string());

    if let Some(url) = requested.map(str::trim).filter(|u| !u.is_empty()) {
        if is_android_anisette_server(url) {
            warnings.push(format!(
                "anisette server {url} generates Android anisette, which Apple refuses since 2026-10-12 (error -80045): using {DEFAULT_ANISETTE_URL_V3} instead"
            ));
        } else if !same_server(url, DEFAULT_ANISETTE_URL_V3) {
            primary = url.trim_end_matches('/').to_string();
            fallback = None;
        }
    }

    match fallback_override.map(str::trim) {
        Some(f) if f.eq_ignore_ascii_case("none") || f.eq_ignore_ascii_case("off") => fallback = None,
        Some(f) if !f.is_empty() => fallback = Some(f.trim_end_matches('/').to_string()),
        _ => {}
    }
    if fallback.as_deref().is_some_and(|f| same_server(f, &primary)) {
        fallback = None;
    }

    AnisetteServers {
        primary,
        fallback,
        warnings,
    }
}

/// Servers to try, in order: the primary then the fallback, the one that served last (`preferred`)
/// first.
pub fn server_order(primary: &str, fallback: Option<&str>, preferred: Option<&str>) -> Vec<String> {
    let mut order = vec![primary.to_string()];
    if let Some(fallback) = fallback.filter(|f| *f != primary) {
        order.push(fallback.to_string());
    }
    if let Some(preferred) = preferred
        && let Some(position) = order.iter().position(|server| server == preferred)
        && position > 0
    {
        let server = order.remove(position);
        order.insert(0, server);
    }
    order
}

#[derive(Clone, Debug)]
pub struct AnisetteConfiguration {
    anisette_url: String,
    anisette_url_v3: String,
    anisette_fallback_url_v3: Option<String>,
    /// Server that produced the last data of the sign-in this configuration belongs to: tried first
    /// from then on (see [`AnisetteConfiguration::set_preferred_url_v3`]).
    anisette_preferred_url_v3: Option<String>,
    configuration_path: PathBuf,
    macos_serial: String,
}

impl Default for AnisetteConfiguration {
    fn default() -> Self {
        AnisetteConfiguration::new()
    }
}

impl AnisetteConfiguration {
    pub fn new() -> AnisetteConfiguration {
        let servers = resolve_servers(
            env_url(&["PLUMESIGN_ANISETTE_URL", "ANISETTE_URL_V3"]).as_deref(),
            env_url(&["PLUMESIGN_ANISETTE_FALLBACK_URL"]).as_deref(),
        );
        for warning in &servers.warnings {
            log::warn!("{warning}");
        }
        AnisetteConfiguration {
            anisette_url: env_url(&["ANISETTE_URL"])
                .unwrap_or_else(|| DEFAULT_ANISETTE_URL.to_string()),
            anisette_url_v3: servers.primary,
            anisette_fallback_url_v3: servers.fallback,
            anisette_preferred_url_v3: None,
            configuration_path: PathBuf::new(),
            macos_serial: "0".to_string(),
        }
    }

    pub fn anisette_url(&self) -> &String {
        &self.anisette_url
    }

    /// Anisette v3 server tried first.
    pub fn anisette_url_v3(&self) -> &String {
        &self.anisette_url_v3
    }

    /// Anisette v3 server tried when the first one fails.
    pub fn fallback_url_v3(&self) -> Option<&String> {
        self.anisette_fallback_url_v3.as_ref()
    }

    pub fn configuration_path(&self) -> &PathBuf {
        &self.configuration_path
    }

    /// Use this anisette v3 server (and no fallback unless one is set afterwards).
    pub fn set_anisette_url_v3(mut self, url: String) -> AnisetteConfiguration {
        self.anisette_url_v3 = url.trim_end_matches('/').to_string();
        self.anisette_fallback_url_v3 = None;
        self.anisette_preferred_url_v3 = None;
        self
    }

    pub fn set_fallback_url_v3(mut self, url: Option<String>) -> AnisetteConfiguration {
        self.anisette_fallback_url_v3 = url;
        self
    }

    /// Anisette v3 server tried first: the one that served the previous data of the same sign-in, so
    /// that a dead default server costs its connection timeout once, not at every refresh.
    pub fn preferred_url_v3(&self) -> Option<&String> {
        self.anisette_preferred_url_v3.as_ref()
    }

    pub fn set_preferred_url_v3(mut self, url: Option<String>) -> AnisetteConfiguration {
        self.anisette_preferred_url_v3 = url;
        self
    }

    /// The anisette v3 servers in the order they are tried.
    pub fn servers_v3(&self) -> Vec<String> {
        server_order(
            &self.anisette_url_v3,
            self.anisette_fallback_url_v3.as_deref(),
            self.anisette_preferred_url_v3.as_deref(),
        )
    }

    /// The other configured server is tried first from now on: Apple kept refusing the data of the
    /// one that produced them. False (nothing changes) when there is no other server.
    pub fn prefer_other_server_v3(&mut self) -> bool {
        let order = self.servers_v3();
        match order.get(1) {
            Some(other) => {
                log::warn!("Apple refuses the data of {}: trying {other} first from now on", order[0]);
                self.anisette_preferred_url_v3 = Some(other.clone());
                true
            }
            None => false,
        }
    }

    pub fn set_anisette_url(mut self, anisette_url: String) -> AnisetteConfiguration {
        self.anisette_url = anisette_url;
        self
    }

    pub fn set_macos_serial(mut self, macos_serial: String) -> AnisetteConfiguration {
        self.macos_serial = macos_serial;
        self
    }

    pub fn set_configuration_path(mut self, configuration_path: PathBuf) -> AnisetteConfiguration {
        self.configuration_path = configuration_path;
        self
    }
}

pub enum AnisetteHeadersProviderType {
    Local,
    Remote,
}

pub struct AnisetteHeadersProviderRes {
    pub provider: Box<dyn AnisetteHeadersProvider>,
    pub provider_type: AnisetteHeadersProviderType,
}

impl AnisetteHeadersProviderRes {
    pub fn local(provider: Box<dyn AnisetteHeadersProvider>) -> AnisetteHeadersProviderRes {
        AnisetteHeadersProviderRes {
            provider,
            provider_type: AnisetteHeadersProviderType::Local,
        }
    }

    pub fn remote(provider: Box<dyn AnisetteHeadersProvider>) -> AnisetteHeadersProviderRes {
        AnisetteHeadersProviderRes {
            provider,
            provider_type: AnisetteHeadersProviderType::Remote,
        }
    }
}

impl AnisetteHeaders {
    pub fn get_anisette_headers_provider(
        configuration: AnisetteConfiguration,
    ) -> Result<AnisetteHeadersProviderRes, AnisetteError> {
        // TODO: handle Err because it will just go to remote anisette and not tell the user anything
        if let Ok(ssc_anisette_headers_provider) =
            AnisetteHeaders::get_ssc_anisette_headers_provider(configuration.clone())
        {
            return Ok(ssc_anisette_headers_provider);
        }

        #[cfg(feature = "remote-anisette-v3")]
        return Ok(AnisetteHeadersProviderRes::remote(Box::new(
            remote_anisette_v3::RemoteAnisetteProviderV3::new(
                configuration.anisette_url_v3,
                configuration.configuration_path.clone(),
                configuration.macos_serial.clone(),
            )
            .with_fallback(configuration.anisette_fallback_url_v3)
            .with_preferred(configuration.anisette_preferred_url_v3),
        )));
    }

    pub fn get_ssc_anisette_headers_provider(
        configuration: AnisetteConfiguration,
    ) -> Result<AnisetteHeadersProviderRes, AnisetteError> {
        let mut ssc_adi_proxy = store_services_core::StoreServicesCoreADIProxy::new(
            configuration.configuration_path(),
        )?;
        let config_path = configuration.configuration_path();
        ssc_adi_proxy.set_provisioning_path(config_path.to_str().ok_or(
            AnisetteError::InvalidArgument("configuration.configuration_path".to_string()),
        )?)?;
        Ok(AnisetteHeadersProviderRes::local(Box::new(
            ADIProxyAnisetteProvider::new(ssc_adi_proxy, config_path.to_path_buf())?,
        )))
    }
}

/// Forget the anisette identity (device identifier and provisioning) so that the next request makes
/// and provisions a new one. Used when Apple or the server says the identity is not valid.
#[cfg(feature = "remote-anisette-v3")]
pub fn reset_identity(configuration_path: &std::path::Path) -> Result<(), AnisetteError> {
    remote_anisette_v3::reset_identity(configuration_path)
}

#[cfg(test)]
mod server_tests {
    use super::*;

    #[test]
    fn host_of_a_url() {
        assert_eq!(url_host("https://ani.sidestore.app").as_deref(), Some("ani.sidestore.app"));
        assert_eq!(url_host("https://ANI.SideStore.app:443/v3/x?y#z").as_deref(), Some("ani.sidestore.app"));
        assert_eq!(url_host("http://user:pw@ani.sidestore.app/").as_deref(), Some("ani.sidestore.app"));
        assert_eq!(url_host("ani.sidestore.app").as_deref(), Some("ani.sidestore.app"));
        assert_eq!(url_host("ani.sidestore.app:8080/x").as_deref(), Some("ani.sidestore.app"));
        assert_eq!(url_host("https://ani.sidestore.app./").as_deref(), Some("ani.sidestore.app"));
        assert_eq!(url_host("http://[::1]:6969/").as_deref(), Some("[::1]"));
        assert_eq!(url_host(""), None);
        assert_eq!(url_host("https://"), None);
    }

    #[test]
    fn android_servers_are_recognised_by_host_only() {
        for host in ANDROID_ANISETTE_HOSTS {
            assert!(is_android_anisette_server(&format!("https://{host}")));
            assert!(is_android_anisette_server(&format!("https://{host}/")));
            assert!(is_android_anisette_server(&format!("HTTP://{}:443", host.to_uppercase())));
        }
        assert!(!is_android_anisette_server("https://ani.jkcoxson.com"));
        assert!(!is_android_anisette_server("https://ani2.stikstore.app"));
        // a lookalike is not the real thing
        assert!(!is_android_anisette_server("https://ani.sidestore.app.example.com"));
        assert!(!is_android_anisette_server("https://example.com/ani.sidestore.app"));
        assert!(!is_android_anisette_server("https://myani.sidestore.app"));
    }

    #[test]
    fn nothing_requested_uses_the_default_and_its_fallback() {
        let s = resolve_servers(None, None);
        assert_eq!(s.primary, DEFAULT_ANISETTE_URL_V3);
        assert_eq!(s.fallback.as_deref(), Some(FALLBACK_ANISETTE_URL_V3));
        assert!(s.warnings.is_empty());
        assert_eq!(resolve_servers(Some("  "), None), s);
    }

    #[test]
    fn android_server_is_replaced_by_the_default() {
        let s = resolve_servers(Some("https://ani.sidestore.app"), None);
        assert_eq!(s.primary, DEFAULT_ANISETTE_URL_V3);
        assert_eq!(s.fallback.as_deref(), Some(FALLBACK_ANISETTE_URL_V3));
        assert_eq!(s.warnings.len(), 1);
        assert!(s.warnings[0].contains("ani.sidestore.app") && s.warnings[0].contains(DEFAULT_ANISETTE_URL_V3));
    }

    #[test]
    fn default_asked_explicitly_keeps_the_fallback() {
        for url in ["https://ani.jkcoxson.com", "https://ani.jkcoxson.com/", "HTTPS://ANI.JKCOXSON.COM"] {
            let s = resolve_servers(Some(url), None);
            assert_eq!(s.primary, DEFAULT_ANISETTE_URL_V3, "{url}");
            assert_eq!(s.fallback.as_deref(), Some(FALLBACK_ANISETTE_URL_V3), "{url}");
            assert!(s.warnings.is_empty());
        }
    }

    #[test]
    fn custom_server_is_kept_alone() {
        let s = resolve_servers(Some("https://anisette.example.org/"), None);
        assert_eq!(s.primary, "https://anisette.example.org");
        assert_eq!(s.fallback, None);
        assert!(s.warnings.is_empty());
    }

    #[test]
    fn the_server_that_served_last_goes_first() {
        let p = "https://a.example";
        let f = "https://b.example";
        assert_eq!(server_order(p, Some(f), None), vec![p, f]);
        assert_eq!(server_order(p, Some(f), Some(p)), vec![p, f]);
        assert_eq!(server_order(p, Some(f), Some(f)), vec![f, p]);
        // a server that is not configured is ignored
        assert_eq!(server_order(p, Some(f), Some("https://c.example")), vec![p, f]);
        assert_eq!(server_order(p, None, Some(f)), vec![p]);
        assert_eq!(server_order(p, Some(p), None), vec![p]);
    }

    #[test]
    fn the_other_server_goes_first_after_a_refusal() {
        // (set explicitly: the default configuration reads the environment of whoever runs the tests)
        let mut config = AnisetteConfiguration::default()
            .set_anisette_url_v3(DEFAULT_ANISETTE_URL_V3.to_string())
            .set_fallback_url_v3(Some(FALLBACK_ANISETTE_URL_V3.to_string()));
        // default pair: primary first, then the fallback, then the primary again
        assert_eq!(config.servers_v3(), vec![DEFAULT_ANISETTE_URL_V3, FALLBACK_ANISETTE_URL_V3]);
        assert!(config.prefer_other_server_v3());
        assert_eq!(config.servers_v3(), vec![FALLBACK_ANISETTE_URL_V3, DEFAULT_ANISETTE_URL_V3]);
        assert_eq!(config.preferred_url_v3().map(String::as_str), Some(FALLBACK_ANISETTE_URL_V3));
        assert!(config.prefer_other_server_v3());
        assert_eq!(config.servers_v3(), vec![DEFAULT_ANISETTE_URL_V3, FALLBACK_ANISETTE_URL_V3]);

        // a server chosen by the user has no other: nothing changes
        let mut alone = AnisetteConfiguration::default().set_anisette_url_v3("https://own.example/".into());
        assert!(!alone.prefer_other_server_v3());
        assert_eq!(alone.servers_v3(), vec!["https://own.example"]);
        assert!(alone.preferred_url_v3().is_none());

        // choosing another server forgets the preference
        let reset = config.set_anisette_url_v3("https://own.example".into());
        assert!(reset.preferred_url_v3().is_none());
    }

    #[test]
    fn fallback_override() {
        let s = resolve_servers(None, Some("https://other.example.org/"));
        assert_eq!(s.fallback.as_deref(), Some("https://other.example.org"));
        for off in ["none", "NONE", "off"] {
            assert_eq!(resolve_servers(None, Some(off)).fallback, None, "{off}");
        }
        let custom = resolve_servers(Some("https://anisette.example.org"), Some("https://ani2.stikstore.app"));
        assert_eq!(custom.fallback.as_deref(), Some("https://ani2.stikstore.app"));
        // a fallback equal to the primary is no fallback
        assert_eq!(resolve_servers(Some("https://ani2.stikstore.app"), Some("https://ani2.stikstore.app/")).fallback, None);
    }
}

#[cfg(test)]
mod tests {
    use log::LevelFilter;
    use simplelog::{ColorChoice, ConfigBuilder, TermLogger, TerminalMode};

    pub fn init_logger() {
        if TermLogger::init(
            LevelFilter::Trace,
            ConfigBuilder::new()
                .set_target_level(LevelFilter::Error)
                .add_filter_allow_str("omnisette")
                .build(),
            TerminalMode::Mixed,
            ColorChoice::Auto,
        )
        .is_ok()
        {}
    }

    #[cfg(not(feature = "async"))]
    #[test]
    fn fetch_anisette_auto() -> Result<()> {
        use crate::{AnisetteConfiguration, AnisetteHeaders};
        use log::info;
        use std::path::PathBuf;

        crate::tests::init_logger();

        let mut provider = AnisetteHeaders::get_anisette_headers_provider(
            AnisetteConfiguration::new()
                .set_configuration_path(PathBuf::new().join("anisette_test")),
        )?;
        info!(
            "Headers: {:?}",
            provider.provider.get_authentication_headers()?
        );
        Ok(())
    }
}
