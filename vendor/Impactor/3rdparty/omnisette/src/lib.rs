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
    /// The provider cannot synchronize with a SIM (hsc 434).
    #[error("anisette resync is not supported by this provider")]
    ResyncUnsupported,
    /// Every candidate server failed.
    #[error("no anisette server could be used: {0}")]
    AllServersFailed(String),
}

pub const DEFAULT_ANISETTE_URL: &str = "https://ani.f1sh.me/";

/// Anisette v3 servers tried when none is configured, in this order. The first one that answers is
/// used, and Apple's answer decides whether its data are good: when Apple refuses them (or the server
/// fails) the next one is tried. The server that worked last goes first the next time.
///
/// Nothing here says which platform Apple accepts: the ecosystem moves every few weeks (Android
/// anisette until 2026-10-12, Windows until 2026-10-22, macOS since), so the data of a server are
/// sent with the client info and user agent the server announces, whatever they say.
pub const DEFAULT_ANISETTE_SERVERS_V3: &[&str] = &[
    "https://mac-ani.jkcoxson.com",
    "https://ani.iloader.app",
    "https://new.ani.sidestore.io",
    "https://ani3.stikstore.app",
    "https://ani.jkcoxson.com",
];

/// The first of [`DEFAULT_ANISETTE_SERVERS_V3`].
pub const DEFAULT_ANISETTE_URL_V3: &str = DEFAULT_ANISETTE_SERVERS_V3[0];

/// Environment variables that name the anisette v3 server to try first.
pub const SERVER_ENV: &[&str] = &["PLUMESIGN_ANISETTE_URL", "ANISETTE_URL_V3"];
/// Servers tried behind the first one, instead of [`DEFAULT_ANISETTE_SERVERS_V3`]: URLs separated by
/// commas, semicolons or spaces; `none` or `off` for no other server.
pub const FALLBACK_ENV: &str = "PLUMESIGN_ANISETTE_FALLBACK_URL";
/// Advanced escape hatch: the `X-Mme-Client-Info` sent to Apple instead of the one the server announces.
pub const CLIENT_INFO_ENV: &str = "PLUMESIGN_ANISETTE_CLIENT_INFO";
/// Advanced escape hatch: the `User-Agent` sent to Apple instead of the one the server announces.
pub const USER_AGENT_ENV: &str = "PLUMESIGN_ANISETTE_USER_AGENT";
/// Accepted and ignored. It used to let a server through whatever platform it announced; every server
/// is used now.
pub const ALLOW_ANY_PLATFORM_ENV: &str = "PLUMESIGN_ANISETTE_ALLOW_ANY_PLATFORM";

/// First non-empty URL among the given environment variables, in order.
fn env_url(names: &[&str]) -> Option<String> {
    names.iter().find_map(|name| {
        std::env::var(name)
            .ok()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
    })
}

/// A server address as it is used: no spaces, no trailing slash, `https://` when no scheme is given.
pub fn normalize_server(url: &str) -> String {
    let url = url.trim().trim_end_matches('/');
    if url.contains("://") {
        url.to_string()
    } else {
        format!("https://{url}")
    }
}

/// The same server, whatever the case or the trailing slash.
pub fn same_server(a: &str, b: &str) -> bool {
    normalize_server(a).eq_ignore_ascii_case(&normalize_server(b))
}

/// Server addresses separated by commas, semicolons or white space.
fn split_servers(list: &str) -> Vec<String> {
    list.split(|c: char| c == ',' || c == ';' || c.is_whitespace())
        .filter(|s| !s.is_empty())
        .map(normalize_server)
        .collect()
}

/// `servers` without the servers that are already there (first one wins).
fn dedupe_servers(servers: Vec<String>) -> Vec<String> {
    let mut unique: Vec<String> = Vec::new();
    for server in servers {
        if !unique.iter().any(|known| same_server(known, &server)) {
            unique.push(server);
        }
    }
    unique
}

/// Decide which servers to use, in order.
///
/// - nothing requested: [`DEFAULT_ANISETTE_SERVERS_V3`];
/// - a server requested (the user's own): tried first, the default servers behind it;
/// - `fallback_override`: replaces the servers behind the first one (URLs separated by commas,
///   semicolons or spaces); `none` or `off` leaves the first one alone.
///
/// No address is refused or replaced because of the host it names: the answer of the server (and then
/// Apple's) decides.
pub fn resolve_servers(requested: Option<&str>, fallback_override: Option<&str>) -> Vec<String> {
    let first = requested
        .map(str::trim)
        .filter(|u| !u.is_empty())
        .map(normalize_server)
        .unwrap_or_else(|| DEFAULT_ANISETTE_URL_V3.to_string());
    let mut servers = vec![first];
    match fallback_override.map(str::trim) {
        Some(f) if f.eq_ignore_ascii_case("none") || f.eq_ignore_ascii_case("off") => {}
        Some(f) if !f.is_empty() => servers.extend(split_servers(f)),
        _ => servers.extend(DEFAULT_ANISETTE_SERVERS_V3.iter().map(|s| s.to_string())),
    }
    dedupe_servers(servers)
}

/// Servers to try, in order: `servers` without the `avoid`ed ones, the `preferred` one (the one that
/// served or worked last) first when it is among them.
pub fn server_order(servers: &[String], preferred: Option<&str>, avoid: &[String]) -> Vec<String> {
    let mut order: Vec<String> = servers
        .iter()
        .filter(|server| !avoid.iter().any(|refused| same_server(refused, server)))
        .cloned()
        .collect();
    if let Some(preferred) = preferred
        && let Some(position) = order.iter().position(|server| same_server(server, preferred))
        && position > 0
    {
        let server = order.remove(position);
        order.insert(0, server);
    }
    order
}

/// The client info and the user agent to send to Apple instead of the ones the anisette server
/// announces: an advanced escape hatch for the day Apple wants another pair than the one a server
/// announces (they always travel as a pair; override both when in doubt). Empty by default.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ClientOverrides {
    pub client_info: Option<String>,
    pub user_agent: Option<String>,
}

/// A value that can be sent as an HTTP header: printable ASCII and spaces. `None` for an empty value
/// or for one that cannot be sent (it is ignored with a warning, not sent half-broken).
fn clean_override(name: &str, value: Option<&str>) -> Option<String> {
    let value = value.map(str::trim).filter(|v| !v.is_empty())?;
    if value.chars().all(|c| c == ' ' || c.is_ascii_graphic()) {
        Some(value.to_string())
    } else {
        log::warn!("{name} is ignored: it holds characters that cannot go in an HTTP header");
        None
    }
}

impl ClientOverrides {
    /// From the given values (an empty or unusable value means no override).
    pub fn new(client_info: Option<&str>, user_agent: Option<&str>) -> ClientOverrides {
        ClientOverrides {
            client_info: clean_override(CLIENT_INFO_ENV, client_info),
            user_agent: clean_override(USER_AGENT_ENV, user_agent),
        }
    }

    /// From `PLUMESIGN_ANISETTE_CLIENT_INFO` and `PLUMESIGN_ANISETTE_USER_AGENT`.
    pub fn from_env() -> ClientOverrides {
        ClientOverrides::new(
            env_url(&[CLIENT_INFO_ENV]).as_deref(),
            env_url(&[USER_AGENT_ENV]).as_deref(),
        )
    }

    pub fn is_empty(&self) -> bool {
        self.client_info.is_none() && self.user_agent.is_none()
    }
}

fn note_ignored_platform_flag() {
    static NOTED: std::sync::Once = std::sync::Once::new();
    NOTED.call_once(|| {
        if std::env::var_os(ALLOW_ANY_PLATFORM_ENV).is_some() {
            log::info!(
                "{ALLOW_ANY_PLATFORM_ENV} is not needed any more and is ignored: every anisette server is used, whatever platform it announces"
            );
        }
    });
}

#[derive(Clone, Debug)]
pub struct AnisetteConfiguration {
    anisette_url: String,
    /// The anisette v3 servers to try, in order (never empty).
    anisette_servers_v3: Vec<String>,
    /// Server that produced the last data of the sign-in this configuration belongs to: tried first
    /// from then on (see [`AnisetteConfiguration::set_preferred_url_v3`]).
    anisette_preferred_url_v3: Option<String>,
    /// Servers whose data Apple refused during this sign-in: not used again until the next one.
    anisette_refused_urls_v3: Vec<String>,
    overrides: ClientOverrides,
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
        note_ignored_platform_flag();
        AnisetteConfiguration {
            anisette_url: env_url(&["ANISETTE_URL"]).unwrap_or_else(|| DEFAULT_ANISETTE_URL.to_string()),
            anisette_servers_v3: resolve_servers(
                env_url(SERVER_ENV).as_deref(),
                env_url(&[FALLBACK_ENV]).as_deref(),
            ),
            anisette_preferred_url_v3: None,
            anisette_refused_urls_v3: Vec::new(),
            overrides: ClientOverrides::from_env(),
            configuration_path: PathBuf::new(),
            macos_serial: "0".to_string(),
        }
    }

    pub fn anisette_url(&self) -> &String {
        &self.anisette_url
    }

    /// The anisette v3 server tried first when nothing is known of the others.
    pub fn anisette_url_v3(&self) -> &String {
        &self.anisette_servers_v3[0]
    }

    /// Every configured anisette v3 server, in the order they are tried when nothing is known of how
    /// they behave.
    pub fn configured_servers_v3(&self) -> &[String] {
        &self.anisette_servers_v3
    }

    pub fn configuration_path(&self) -> &PathBuf {
        &self.configuration_path
    }

    /// These anisette v3 servers, in this order (the default servers when the list is empty). What was
    /// learnt of the old ones is forgotten.
    pub fn set_servers_v3(mut self, servers: Vec<String>) -> AnisetteConfiguration {
        let servers = dedupe_servers(
            servers
                .iter()
                .filter(|s| !s.trim().is_empty())
                .map(|s| normalize_server(s))
                .collect(),
        );
        self.anisette_servers_v3 = if servers.is_empty() {
            resolve_servers(None, None)
        } else {
            servers
        };
        self.anisette_preferred_url_v3 = None;
        self.anisette_refused_urls_v3.clear();
        self
    }

    /// Use this anisette v3 server alone.
    pub fn set_anisette_url_v3(self, url: String) -> AnisetteConfiguration {
        self.set_servers_v3(vec![url])
    }

    /// Anisette v3 server tried first: the one that served the previous data of the same sign-in, so
    /// that a dead server costs its connection timeout once, not at every refresh.
    pub fn preferred_url_v3(&self) -> Option<&String> {
        self.anisette_preferred_url_v3.as_ref()
    }

    pub fn set_preferred_url_v3(mut self, url: Option<String>) -> AnisetteConfiguration {
        self.anisette_preferred_url_v3 = url;
        self
    }

    /// The servers in the order they are tried, as this sign-in knows them: the one that served last
    /// first, the ones Apple refused left out.
    pub fn servers_v3(&self) -> Vec<String> {
        server_order(
            &self.anisette_servers_v3,
            self.anisette_preferred_url_v3.as_deref(),
            &self.anisette_refused_urls_v3,
        )
    }

    /// [`Self::servers_v3`], and when this sign-in has no preference yet the server that worked last
    /// time (kept on disk next to the identities, valid for this very list of servers only).
    pub fn ordered_servers_v3(&self) -> Vec<String> {
        let preferred = self
            .anisette_preferred_url_v3
            .clone()
            .or_else(|| self.remembered_server_v3());
        server_order(
            &self.anisette_servers_v3,
            preferred.as_deref(),
            &self.anisette_refused_urls_v3,
        )
    }

    #[cfg(feature = "remote-anisette-v3")]
    fn remembered_server_v3(&self) -> Option<String> {
        remote_anisette_v3::remembered_server(&self.configuration_path, &self.anisette_servers_v3)
    }

    #[cfg(not(feature = "remote-anisette-v3"))]
    fn remembered_server_v3(&self) -> Option<String> {
        None
    }

    /// Apple refused the data of this server: it is not used again by this sign-in. True when another
    /// server is left to try, false (and nothing to switch to) when it was the last one.
    pub fn refuse_server_v3(&mut self, url: &str) -> bool {
        if !self.anisette_refused_urls_v3.iter().any(|known| same_server(known, url)) {
            self.anisette_refused_urls_v3.push(normalize_server(url));
        }
        if self
            .anisette_preferred_url_v3
            .as_deref()
            .is_some_and(|preferred| same_server(preferred, url))
        {
            self.anisette_preferred_url_v3 = None;
        }
        self.anisette_servers_v3
            .iter()
            .any(|server| !self.anisette_refused_urls_v3.iter().any(|refused| same_server(refused, server)))
    }

    /// This configuration with `url` as its only server (an operation that belongs to the identity of
    /// that server, like the synchronization Apple asked for).
    pub fn only_server_v3(&self, url: &str) -> AnisetteConfiguration {
        let mut only = self.clone().set_anisette_url_v3(url.to_string());
        only.anisette_preferred_url_v3 = Some(normalize_server(url));
        only
    }

    /// The client info and user agent sent to Apple instead of the server's own (empty by default).
    pub fn client_overrides(&self) -> &ClientOverrides {
        &self.overrides
    }

    pub fn set_client_overrides(mut self, overrides: ClientOverrides) -> AnisetteConfiguration {
        self.overrides = overrides;
        self
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
            remote_anisette_v3::RemoteAnisetteProviderV3::with_servers(
                configuration.ordered_servers_v3(),
                configuration.configuration_path.clone(),
                configuration.macos_serial.clone(),
            )
            .with_overrides(configuration.overrides.clone()),
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

/// Forget the anisette identity (device identifier and provisioning) of this server so that the next
/// request makes and provisions a new one. Used when Apple or the server says the identity is not
/// valid. The identities of the other servers are not touched.
#[cfg(feature = "remote-anisette-v3")]
pub fn reset_identity(configuration_path: &std::path::Path, server: &str) -> Result<(), AnisetteError> {
    remote_anisette_v3::reset_identity(configuration_path, server)
}

/// The server Apple accepted the data of, to be tried first the next time (see
/// [`AnisetteConfiguration::ordered_servers_v3`]). `servers` is the list the configuration holds.
/// Best effort: a disk that cannot be written only costs the memory.
#[cfg(feature = "remote-anisette-v3")]
pub fn remember_server(configuration_path: &std::path::Path, servers: &[String], server: &str) {
    remote_anisette_v3::remember_server(configuration_path, servers, server)
}

#[cfg(test)]
mod server_tests {
    use super::*;

    fn defaults() -> Vec<String> {
        DEFAULT_ANISETTE_SERVERS_V3.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn the_default_servers_are_these_in_this_order() {
        assert_eq!(
            DEFAULT_ANISETTE_SERVERS_V3,
            [
                "https://mac-ani.jkcoxson.com",
                "https://ani.iloader.app",
                "https://new.ani.sidestore.io",
                "https://ani3.stikstore.app",
                "https://ani.jkcoxson.com",
            ]
        );
        assert_eq!(DEFAULT_ANISETTE_URL_V3, "https://mac-ani.jkcoxson.com");
    }

    #[test]
    fn nothing_requested_uses_the_default_servers_in_order() {
        assert_eq!(resolve_servers(None, None), defaults());
        assert_eq!(resolve_servers(Some("  "), Some("")), defaults());
    }

    #[test]
    fn the_server_of_the_user_goes_first_and_the_defaults_follow() {
        let servers = resolve_servers(Some("https://anisette.example.org/"), None);
        assert_eq!(servers[0], "https://anisette.example.org");
        assert_eq!(servers[1..], defaults()[..]);

        // a default asked explicitly moves to the front, once
        let servers = resolve_servers(Some("HTTPS://ANI.IloaDer.app/"), None);
        assert_eq!(servers.len(), defaults().len());
        assert!(servers[0].eq_ignore_ascii_case("https://ani.iloader.app"));
        assert_eq!(servers[1], "https://mac-ani.jkcoxson.com");
        assert_eq!(servers.iter().filter(|s| s.eq_ignore_ascii_case("https://ani.iloader.app")).count(), 1);
    }

    #[test]
    fn no_host_is_turned_down_by_its_name() {
        // the old Android servers, and the Windows ones that were the defaults of 1.0.1, are servers
        // like any other: what they answer decides
        for url in [
            "https://ani.sidestore.app",
            "https://ani.stikstore.app",
            "https://anisette.crystall1ne.dev",
            "https://ani3server.fly.dev",
            "https://ani.jaydenha.uk",
            "https://omni.ethxn.xyz",
            "https://ani2.stikstore.app",
        ] {
            let servers = resolve_servers(Some(url), None);
            assert_eq!(servers[0], url);
            assert_eq!(servers[1..], defaults()[..], "{url}");
        }
    }

    #[test]
    fn a_server_without_scheme_is_https() {
        assert_eq!(normalize_server("ani.example.org"), "https://ani.example.org");
        assert_eq!(normalize_server(" http://127.0.0.1:6969/ "), "http://127.0.0.1:6969");
        assert!(same_server("https://A.example/", "a.example"));
        assert!(!same_server("https://a.example", "https://b.example"));
    }

    #[test]
    fn fallback_none_or_off_leaves_the_first_server_alone() {
        for off in ["none", "NONE", " off "] {
            assert_eq!(resolve_servers(Some("https://own.example"), Some(off)), ["https://own.example"], "{off}");
            assert_eq!(resolve_servers(None, Some(off)), [DEFAULT_ANISETTE_URL_V3], "{off}");
        }
    }

    #[test]
    fn the_fallback_override_replaces_the_default_servers_behind_the_first() {
        assert_eq!(
            resolve_servers(Some("https://own.example"), Some("https://b.example/")),
            ["https://own.example", "https://b.example"]
        );
        assert_eq!(
            resolve_servers(None, Some("https://b.example, https://c.example;https://d.example  https://e.example")),
            [DEFAULT_ANISETTE_URL_V3, "https://b.example", "https://c.example", "https://d.example", "https://e.example"]
        );
        // a server named twice is tried once
        assert_eq!(
            resolve_servers(Some("https://b.example"), Some("https://B.example/ https://c.example")),
            ["https://b.example", "https://c.example"]
        );
    }

    #[test]
    fn the_server_that_served_last_goes_first() {
        let servers: Vec<String> = ["https://a.example", "https://b.example", "https://c.example"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        assert_eq!(server_order(&servers, None, &[]), servers);
        assert_eq!(server_order(&servers, Some("https://a.example"), &[]), servers);
        assert_eq!(
            server_order(&servers, Some("https://C.example/"), &[]),
            ["https://c.example", "https://a.example", "https://b.example"]
        );
        // a server that is not configured is ignored
        assert_eq!(server_order(&servers, Some("https://z.example"), &[]), servers);
        // a refused server is left out, even when it was the preferred one
        let refused = vec!["https://c.example".to_string()];
        assert_eq!(server_order(&servers, Some("https://c.example"), &refused), ["https://a.example", "https://b.example"]);
    }

    #[test]
    fn a_server_apple_refused_is_not_asked_again_by_the_sign_in() {
        // (set explicitly: the default configuration reads the environment of whoever runs the tests)
        let mut config = AnisetteConfiguration::default().set_servers_v3(vec![
            "https://a.example".into(),
            "https://b.example".into(),
            "https://c.example".into(),
        ]);
        assert_eq!(config.servers_v3(), ["https://a.example", "https://b.example", "https://c.example"]);
        assert!(config.refuse_server_v3("https://a.example"));
        assert_eq!(config.servers_v3(), ["https://b.example", "https://c.example"]);
        config = config.set_preferred_url_v3(Some("https://c.example".into()));
        assert_eq!(config.servers_v3(), ["https://c.example", "https://b.example"]);
        assert!(config.refuse_server_v3("https://c.example/"));
        assert_eq!(config.servers_v3(), ["https://b.example"]);
        assert!(config.preferred_url_v3().is_none());
        // the last one: nothing left to switch to
        assert!(!config.refuse_server_v3("https://b.example"));
        assert!(config.servers_v3().is_empty());
        // refusing it again changes nothing
        assert!(!config.refuse_server_v3("https://b.example"));

        // choosing the servers again forgets all of it
        let fresh = config.set_servers_v3(vec!["https://own.example/".into()]);
        assert_eq!(fresh.servers_v3(), ["https://own.example"]);
        assert!(fresh.preferred_url_v3().is_none());
        // an empty list is the default servers, never no server
        assert_eq!(fresh.set_servers_v3(Vec::new()).configured_servers_v3(), defaults());
    }

    #[test]
    fn one_server_chosen_is_used_alone() {
        let config = AnisetteConfiguration::default().set_anisette_url_v3("https://own.example/".into());
        assert_eq!(config.configured_servers_v3(), ["https://own.example"]);
        assert_eq!(config.anisette_url_v3(), "https://own.example");
    }

    #[test]
    fn only_one_server_for_an_operation_of_its_identity() {
        let config = AnisetteConfiguration::default()
            .set_servers_v3(vec!["https://a.example".into(), "https://b.example".into()]);
        let only = config.only_server_v3("https://b.example");
        assert_eq!(only.servers_v3(), ["https://b.example"]);
        assert_eq!(config.servers_v3(), ["https://a.example", "https://b.example"]);
    }

    #[test]
    fn overrides_are_empty_unless_given_and_usable() {
        assert!(ClientOverrides::new(None, None).is_empty());
        assert!(ClientOverrides::new(Some("  "), Some("")).is_empty());
        let o = ClientOverrides::new(
            Some(" <Mac15,7> <macOS;27.0;26A5378j> <com.apple.AuthKit/1 (com.apple.akd/1.0)> "),
            Some("akd/1.0 CFNetwork/811.5.4"),
        );
        assert_eq!(
            o.client_info.as_deref(),
            Some("<Mac15,7> <macOS;27.0;26A5378j> <com.apple.AuthKit/1 (com.apple.akd/1.0)>")
        );
        assert_eq!(o.user_agent.as_deref(), Some("akd/1.0 CFNetwork/811.5.4"));
        // each one on its own
        assert_eq!(ClientOverrides::new(Some("x"), None).user_agent, None);
        // a value that cannot be an HTTP header is dropped, not sent half broken
        assert!(ClientOverrides::new(Some("a\r\nb: c"), Some("caf\u{e9}")).is_empty());
    }

    #[test]
    fn the_platform_flag_of_the_old_gate_is_still_a_known_name() {
        // accepted and ignored: nothing reads it to decide anything
        assert_eq!(ALLOW_ANY_PLATFORM_ENV, "PLUMESIGN_ANISETTE_ALLOW_ANY_PLATFORM");
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
