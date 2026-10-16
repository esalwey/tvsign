use std::io::{BufRead, Write};
use std::net::{IpAddr, Ipv6Addr, SocketAddr, SocketAddrV6};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::Duration;

use anyhow::{Result, bail};
use clap::{Args, Subcommand};
use idevice::IdeviceError;
use idevice::RsdService;
use idevice::installation_proxy::InstallationProxyClient;
use plume_utils::wireless::{
    Hint, MDNS_PORT, PairedDevice, PairingStore, RpPairingFile, RsdConnection, ServiceKind, discover,
    discover_at, locate_and_connect_via, locate_via, pair, pair_usb,
};

use crate::get_data_path;

const DEFAULT_HOST: &str = "plume";

#[derive(Debug, Args)]
pub struct PairArgs {
    #[command(subcommand)]
    pub command: Option<PairCommands>,
    /// IP address of the device to pair with. A link-local IPv6 address needs its interface
    /// index: fe80::1%12 (as printed by `pair discover`).
    #[arg(long = "ip", value_name = "IP", requires = "port", value_parser = parse_scoped_ip)]
    pub ip: Option<ScopedIp>,
    /// Port the device advertises its pairing service on
    #[arg(long = "port", value_name = "PORT", requires = "ip")]
    pub port: Option<u16>,
    /// PIN shown on the device screen (read from stdin if not given)
    #[arg(long = "pin", value_name = "PIN")]
    pub pin: Option<String>,
    /// mDNS identifier of the device being paired, echoed in the PAIRED line
    /// (looked up on the network when omitted)
    #[arg(long = "identifier", value_name = "IDENTIFIER")]
    pub identifier: Option<String>,
    /// Name to present to the device; keep it the same as when pairing
    #[arg(long = "host", value_name = "NAME", default_value = DEFAULT_HOST, global = true)]
    pub host: String,
}

#[derive(Debug, Subcommand)]
pub enum PairCommands {
    /// Browse the network for devices that can be paired with
    Discover(DiscoverArgs),
    /// List devices this host has already paired with
    List,
    /// Identify a paired device from its advertised mDNS identifier and auth tag
    Find(FindArgs),
    /// Delete a stored pairing
    Forget(ForgetArgs),
    /// Pair once with an iPhone or iPad plugged in over USB (iOS 17 or later)
    Usb(UsbArgs),
    /// Store a pairing file made elsewhere (Impactor, another computer) for a device
    Import(ImportArgs),
    /// Tell whether Developer Mode is on (`DEVMODE on` / `DEVMODE off`)
    Devmode(TargetArgs),
    /// Print a paired device's properties over the wireless tunnel
    Info(InfoArgs),
    /// Check that a paired device is reachable and still trusts this host
    Check(TargetArgs),
    /// Install an app on a paired device over the wireless tunnel
    Install(InstallArgs),
    /// List the user apps installed on a paired device
    Apps(AppsArgs),
    /// List the processes running on a paired device, with their bundle ID when known
    Processes(ProcessesArgs),
    /// Tell whether an app has a main process on a paired device (exit 0 yes, 4 no). tvOS keeps
    /// suspended apps resident, so this does not mean the app is open in the foreground.
    Running(RunningArgs),
}

/// Exit codes for the wireless commands (`info`, `check`, `install`, `apps`,
/// `processes`, `running`).
pub mod exit_code {
    pub const OK: i32 = 0;
    pub const OTHER: i32 = 1;
    /// The pairing is missing, or the device no longer accepts it.
    pub const PAIRING_LOST: i32 = 2;
    /// No matching device on the network, or it stopped responding.
    pub const NOT_FOUND: i32 = 3;
    /// `running`: the app has no process on the device.
    pub const NOT_RUNNING: i32 = 4;
    /// The device exposes no service able to list processes.
    pub const UNSUPPORTED: i32 = 5;
    /// `pair`: no PIN was given (stdin closed, empty or timed out); nothing reached the device.
    pub const CANCELLED: i32 = 6;
}

/// An IP address that may carry an IPv6 scope id (`fe80::1%12`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ScopedIp {
    pub ip: IpAddr,
    pub scope_id: u32,
}

impl ScopedIp {
    fn socket_addr(self, port: u16) -> SocketAddr {
        match self.ip {
            IpAddr::V6(v6) => SocketAddr::V6(SocketAddrV6::new(v6, port, 0, self.scope_id)),
            ip => SocketAddr::new(ip, port),
        }
    }
}

/// `192.168.1.5`, `2001:db8::1`, or `fe80::1%12` (numeric interface index).
fn parse_scoped_ip(value: &str) -> std::result::Result<ScopedIp, String> {
    let value = value.trim();
    let Some((addr, scope)) = value.split_once('%') else {
        return value
            .parse::<IpAddr>()
            .map(|ip| ScopedIp { ip, scope_id: 0 })
            .map_err(|e| format!("invalid IP address: {e}"));
    };
    let ip: Ipv6Addr = addr
        .parse()
        .map_err(|e| format!("a scope id is only valid on an IPv6 address: {e}"))?;
    let scope_id: u32 = scope.parse().map_err(|_| {
        format!("the scope after % must be the numeric interface index (e.g. {addr}%12), got {scope:?}")
    })?;
    Ok(ScopedIp {
        ip: IpAddr::V6(ip),
        scope_id,
    })
}

/// Address as `pair --ip` expects it: the interface index is kept for IPv6 link-local addresses.
fn format_scoped(address: &SocketAddr) -> String {
    match address {
        SocketAddr::V6(v6) if v6.scope_id() != 0 => format!("{}%{}", v6.ip(), v6.scope_id()),
        other => other.ip().to_string(),
    }
}

#[derive(Debug, Args)]
pub struct TargetArgs {
    /// Stored pairing UDID, or the `identifier` the device advertises over mDNS
    #[arg(
        short = 'i',
        long = "identifier",
        visible_alias = "udid",
        value_name = "ID",
        required = true
    )]
    pub identifier: String,
    /// How long to look for the device on the network, in seconds
    #[arg(
        short = 't',
        long = "timeout",
        value_name = "SECONDS",
        default_value_t = 10
    )]
    pub timeout: u64,
    /// Ask this address directly (unicast mDNS) before browsing the network: finds a device whose
    /// multicast announcements do not reach this host (VLAN, Docker bridge network, firewall)
    #[arg(long = "hint-ip", value_name = "IP", value_parser = parse_scoped_ip)]
    pub hint_ip: Option<ScopedIp>,
    /// mDNS port to query on the hint address
    #[arg(long = "hint-port", value_name = "PORT", default_value_t = MDNS_PORT, hide = true)]
    pub hint_port: u16,
}

impl TargetArgs {
    fn hint(&self) -> Option<Hint> {
        self.hint_ip
            .map(|ip| Hint::at(ip.socket_addr(self.hint_port)))
    }
}

#[derive(Debug, Args)]
pub struct UsbArgs {
    /// UDID of the device to pair (the only one plugged in when omitted)
    #[arg(short = 'u', long = "udid", value_name = "UDID")]
    pub udid: Option<String>,
}

#[derive(Debug, Args)]
pub struct ImportArgs {
    /// Pairing file (.plist)
    #[arg(value_name = "FILE", required = true)]
    pub file: std::path::PathBuf,
    /// UDID of the device the file belongs to
    #[arg(short = 'u', long = "udid", value_name = "UDID", required = true)]
    pub udid: String,
    /// Name to remember the device under
    #[arg(short = 'n', long = "name", value_name = "NAME")]
    pub name: Option<String>,
    /// Model identifier, e.g. iPhone15,2
    #[arg(short = 'm', long = "model", value_name = "MODEL")]
    pub model: Option<String>,
}

#[derive(Debug, Args)]
pub struct InfoArgs {
    #[command(flatten)]
    pub target: TargetArgs,
    /// Print a single JSON object
    #[arg(long = "json")]
    pub json: bool,
}

#[derive(Debug, Args)]
pub struct InstallArgs {
    #[command(flatten)]
    pub target: TargetArgs,
    /// Signed .ipa (or .app directory) to install
    #[arg(value_name = "PACKAGE", required = true)]
    pub package: std::path::PathBuf,
    /// Print a `WAITING <percent> <seconds>` line when installd, once the package
    /// is uploaded, makes no progress for this many seconds (0 disables). installd
    /// holds an update while the app is in use on the device.
    #[arg(long = "stall", value_name = "SECONDS", default_value_t = 45)]
    pub stall: u64,
}

#[derive(Debug, Args)]
pub struct ProcessesArgs {
    #[command(flatten)]
    pub target: TargetArgs,
    /// Also list processes that belong to no installed app (daemons)
    #[arg(long = "all")]
    pub all: bool,
    /// Print one JSON object per process
    #[arg(long = "json")]
    pub json: bool,
}

#[derive(Debug, Args)]
pub struct RunningArgs {
    #[command(flatten)]
    pub target: TargetArgs,
    /// Bundle identifier of the app
    #[arg(
        short = 'b',
        long = "bundle-id",
        value_name = "BUNDLE_ID",
        required = true
    )]
    pub bundle_id: String,
}

#[derive(Debug, Args)]
pub struct AppsArgs {
    #[command(flatten)]
    pub target: TargetArgs,
    /// Only show this bundle identifier
    #[arg(short = 'b', long = "bundle-id", value_name = "BUNDLE_ID")]
    pub bundle_id: Option<String>,
    /// Print one JSON object per app
    #[arg(long = "json")]
    pub json: bool,
}

#[derive(Debug, Args)]
pub struct DiscoverArgs {
    /// How long to browse for, in seconds
    #[arg(
        short = 't',
        long = "timeout",
        value_name = "SECONDS",
        default_value_t = 5
    )]
    pub timeout: u64,
    /// Print one JSON object per device instead of text
    #[arg(long = "json")]
    pub json: bool,
    /// Query this address directly (unicast mDNS) instead of browsing the network: for a device
    /// whose multicast announcements do not reach this host
    #[arg(long = "ip", value_name = "IP", value_parser = parse_scoped_ip)]
    pub ip: Option<ScopedIp>,
    /// mDNS port to query on that address
    #[arg(long = "mdns-port", value_name = "PORT", default_value_t = MDNS_PORT, hide = true)]
    pub mdns_port: u16,
}

#[derive(Debug, Args)]
pub struct FindArgs {
    /// `identifier` TXT record from the device's _remotepairing._tcp service
    #[arg(long = "identifier", value_name = "IDENTIFIER", required = true)]
    pub identifier: String,
    /// `authTag` TXT record from the device's _remotepairing._tcp service
    #[arg(long = "auth-tag", value_name = "AUTH_TAG", required = true)]
    pub auth_tag: String,
}

#[derive(Debug, Args)]
pub struct ForgetArgs {
    /// UDID of the device to forget
    #[arg(short = 'u', long = "udid", value_name = "UDID", required = true)]
    pub udid: String,
}

pub async fn execute(args: PairArgs) -> Result<()> {
    let store = PairingStore::new(get_data_path().join("remotepairing"));

    match args.command {
        Some(PairCommands::Discover(discover_args)) => run_discover(&store, discover_args).await,
        Some(PairCommands::List) => run_list(&store).await,
        Some(PairCommands::Find(find_args)) => run_find(&store, find_args).await,
        Some(PairCommands::Forget(forget_args)) => run_forget(&store, forget_args).await,
        Some(PairCommands::Usb(usb_args)) => {
            exit_with(run_usb(&store, &args.host, usb_args).await)
        }
        Some(PairCommands::Import(import_args)) => {
            exit_with(run_import(&store, &args.host, import_args).await)
        }
        Some(PairCommands::Devmode(target)) => {
            exit_with(run_devmode(&store, &args.host, target).await)
        }
        Some(PairCommands::Info(info_args)) => {
            exit_with(run_info(&store, &args.host, info_args).await)
        }
        Some(PairCommands::Check(target)) => exit_with(run_check(&store, &args.host, target).await),
        Some(PairCommands::Install(install_args)) => {
            exit_with(run_install(&store, &args.host, install_args).await)
        }
        Some(PairCommands::Apps(apps_args)) => {
            exit_with(run_apps(&store, &args.host, apps_args).await)
        }
        Some(PairCommands::Processes(processes_args)) => {
            exit_with(run_processes(&store, &args.host, processes_args).await)
        }
        Some(PairCommands::Running(running_args)) => {
            exit_with_code(run_running(&store, &args.host, running_args).await)
        }
        None => run_pair(&store, args).await,
    }
}

async fn run_pair(store: &PairingStore, args: PairArgs) -> Result<()> {
    let (Some(ip), Some(port)) = (args.ip, args.port) else {
        bail!("--ip and --port are required to pair; run `plumesign pair discover` to find them");
    };
    let address = ip.socket_addr(port);

    log::info!("Pairing with {address} as {:?}", args.host);

    let (pairing_file, device) = pair(address, &args.host, || {
        let pin = args.pin.clone();
        async move {
            match pin {
                // Un PIN vide ne part jamais vers la TV : ce serait une tentative ratee de plus.
                Some(pin) => normalize_pin(&pin).unwrap_or_else(|| cancel_pin("empty PIN")),
                None => read_pin(),
            }
        }
    })
    .await?;

    let path = store.save(&device, &pairing_file).await?;

    log::info!(
        "Paired with {} ({}), UDID {}, saved to {}",
        device.name,
        device.model,
        device.udid,
        path.display()
    );

    // Report the identifier the device advertises once paired, proven by its
    // auth tag, since that is what later commands are given.
    let identifier = match locate_via(
        store,
        &device.udid,
        Some(Hint::at(ip.socket_addr(MDNS_PORT))),
        Duration::from_secs(5),
    )
    .await
    {
        Ok(located) => Some(located.service_identifier),
        Err(e) => {
            log::warn!("Could not see the paired device advertise itself yet: {e}");
            args.identifier
        }
    };

    println!(
        "PAIRED {} {}",
        identifier.as_deref().unwrap_or("-"),
        device.udid
    );
    flush_stdout();

    Ok(())
}

/// Pairs with a device plugged in over USB and stores the pairing. Prints one JSON line with what
/// lockdown reported, then `PAIRED - <udid>` (no identifier: the device is not on mDNS yet).
async fn run_usb(store: &PairingStore, host: &str, args: UsbArgs) -> Result<()> {
    log::info!("Pairing over USB as {host:?}");
    let paired = pair_usb(args.udid.as_deref(), host).await?;
    let path = store.save(&paired.device, &paired.pairing_file).await?;
    log::info!(
        "Paired with {} ({}), UDID {}, saved to {}",
        paired.device.name,
        paired.device.model,
        paired.device.udid,
        path.display()
    );
    println!(
        "{}",
        serde_json::json!({
            "UniqueDeviceID": paired.device.udid,
            "DeviceName": paired.info.name,
            "ProductType": paired.info.product_type,
            "ProductVersion": paired.info.product_version,
            "DeviceFamily": device_family(None, paired.info.product_type.as_deref()),
        })
    );
    println!("PAIRED - {}", paired.device.udid);
    flush_stdout();
    Ok(())
}

/// Stores a pairing file made elsewhere after checking that it parses.
async fn run_import(store: &PairingStore, host: &str, args: ImportArgs) -> Result<()> {
    let pairing_file = RpPairingFile::read_from_file(&args.file)
        .await
        .map_err(|e| anyhow::anyhow!("Not a valid pairing file ({}): {e}", args.file.display()))?;
    let device = PairedDevice {
        udid: args.udid.clone(),
        name: args.name.clone().unwrap_or_else(|| args.udid.clone()),
        model: args.model.clone().unwrap_or_else(|| "Unknown".to_string()),
        account_id: host.to_string(),
    };
    let path = store.save(&device, &pairing_file).await?;
    log::info!("Imported pairing for {} into {}", device.udid, path.display());
    println!("PAIRED - {}", device.udid);
    flush_stdout();
    Ok(())
}

/// Developer Mode status through the AMFI service of the wireless tunnel.
async fn run_devmode(store: &PairingStore, host: &str, target: TargetArgs) -> Result<()> {
    use idevice::amfi::AmfiClient;

    let mut connection = connect(store, host, &target).await?;
    let mut client =
        AmfiClient::connect_rsd(&mut connection.adapter, &mut connection.handshake).await?;
    let on = client.get_developer_mode_status().await?;
    println!("DEVMODE {}", if on { "on" } else { "off" });
    Ok(())
}

/// Family of a device from its `DeviceClass` or, failing that, its `ProductType`:
/// `tvos`, `ios` (iPhone, iPod touch), `ipados`, `visionos`.
fn device_family(device_class: Option<&str>, product_type: Option<&str>) -> Option<&'static str> {
    [device_class, product_type]
        .into_iter()
        .flatten()
        .find_map(|value| {
            let v = value.to_ascii_lowercase();
            if v.starts_with("appletv") {
                Some("tvos")
            } else if v.starts_with("ipad") {
                Some("ipados")
            } else if v.starts_with("iphone") || v.starts_with("ipod") {
                Some("ios")
            } else if v.starts_with("realitydevice") || v.starts_with("vision") {
                Some("visionos")
            } else {
                None
            }
        })
}

async fn connect(store: &PairingStore, host: &str, target: &TargetArgs) -> Result<RsdConnection> {
    let connection = locate_and_connect_via(
        store,
        &target.identifier,
        host,
        target.hint(),
        Duration::from_secs(target.timeout),
    )
    .await?;

    log::info!(
        "Connected to {} ({}) at {}",
        connection.device.record.name,
        connection.device.record.udid,
        connection.device.address
    );
    Ok(connection)
}

/// First of `keys` present as a scalar in the RSD properties. Some keys moved
/// between OS releases (`ProductVersion` / `OSVersion`).
fn property_string(connection: &RsdConnection, keys: &[&str]) -> Option<String> {
    keys.iter().find_map(|key| {
        connection
            .handshake
            .properties
            .get(*key)
            .and_then(plist_scalar)
    })
}

fn plist_scalar(value: &plist::Value) -> Option<String> {
    match value {
        plist::Value::String(s) => Some(s.clone()),
        plist::Value::Integer(i) => Some(i.to_string()),
        plist::Value::Real(r) => Some(r.to_string()),
        plist::Value::Boolean(b) => Some(b.to_string()),
        _ => None,
    }
}

fn plist_to_json(value: &plist::Value) -> serde_json::Value {
    match value {
        plist::Value::String(s) => s.clone().into(),
        plist::Value::Boolean(b) => (*b).into(),
        plist::Value::Integer(i) => i
            .as_signed()
            .map(Into::into)
            .or_else(|| i.as_unsigned().map(Into::into))
            .unwrap_or(serde_json::Value::Null),
        plist::Value::Real(r) => serde_json::Number::from_f64(*r)
            .map(serde_json::Value::Number)
            .unwrap_or(serde_json::Value::Null),
        plist::Value::Array(a) => a.iter().map(plist_to_json).collect(),
        plist::Value::Dictionary(d) => d
            .iter()
            .map(|(k, v)| (k.clone(), plist_to_json(v)))
            .collect::<serde_json::Map<_, _>>()
            .into(),
        plist::Value::Data(d) => d
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect::<String>()
            .into(),
        plist::Value::Date(d) => d.to_xml_format().into(),
        plist::Value::Uid(u) => u.get().into(),
        _ => serde_json::Value::Null,
    }
}

async fn run_info(store: &PairingStore, host: &str, args: InfoArgs) -> Result<()> {
    let connection = connect(store, host, &args.target).await?;
    let record = &connection.device.record;

    let unique_device_id = property_string(&connection, &["UniqueDeviceID"]);
    let fields = [
        ("UniqueDeviceID", unique_device_id.clone()),
        (
            "ProductType",
            property_string(&connection, &["ProductType"]),
        ),
        (
            "ProductVersion",
            property_string(&connection, &["ProductVersion", "OSVersion"]),
        ),
        (
            "BuildVersion",
            property_string(&connection, &["BuildVersion"]),
        ),
        (
            "DeviceName",
            property_string(&connection, &["DeviceName"]).or_else(|| Some(record.name.clone())),
        ),
        (
            "DeviceClass",
            property_string(&connection, &["DeviceClass"]),
        ),
        (
            "HardwareModel",
            property_string(&connection, &["HardwareModel", "HWModel"]),
        ),
    ];
    let family = device_family(
        property_string(&connection, &["DeviceClass"]).as_deref(),
        property_string(&connection, &["ProductType"]).as_deref(),
    );
    let udid_matches = unique_device_id
        .as_deref()
        .map(|udid| udid.eq_ignore_ascii_case(&record.udid));

    if args.json {
        let mut out = serde_json::Map::new();
        out.insert(
            "identifier".into(),
            connection.device.service_identifier.clone().into(),
        );
        out.insert("pairing_udid".into(), record.udid.clone().into());
        out.insert("udid_matches_pairing".into(), udid_matches.into());
        out.insert("DeviceFamily".into(), family.into());
        out.insert(
            "ip".into(),
            connection.device.address.ip().to_string().into(),
        );
        out.insert("port".into(), connection.device.address.port().into());
        for (key, value) in &fields {
            out.insert((*key).into(), value.clone().into());
        }
        out.insert(
            "properties".into(),
            connection
                .handshake
                .properties
                .iter()
                .map(|(k, v)| (k.clone(), plist_to_json(v)))
                .collect::<serde_json::Map<_, _>>()
                .into(),
        );
        println!("{}", serde_json::Value::Object(out));
    } else {
        println!("Identifier: {}", connection.device.service_identifier);
        println!("PairingUDID: {}", record.udid);
        println!("Address: {}", connection.device.address);
        if let Some(family) = family {
            println!("DeviceFamily: {family}");
        }
        for (key, value) in &fields {
            if let Some(value) = value {
                println!("{key}: {value}");
            }
        }
    }

    Ok(())
}

async fn run_check(store: &PairingStore, host: &str, target: TargetArgs) -> Result<()> {
    let connection = connect(store, host, &target).await?;

    println!(
        "REACHABLE {} {} {} {}",
        connection.device.service_identifier,
        connection.device.record.udid,
        connection.device.address.ip(),
        connection.device.address.port()
    );
    Ok(())
}

async fn run_install(store: &PairingStore, host: &str, args: InstallArgs) -> Result<()> {
    if !args.package.exists() {
        bail!("Package not found: {}", args.package.display());
    }

    let mut connection = connect(store, host, &args.target).await?;
    println!(
        "CONNECTED {} {}",
        connection.device.service_identifier, connection.device.record.udid
    );
    println!("UPLOADING");
    flush_stdout();

    let last = AtomicU64::new(u64::MAX);
    let started = std::time::Instant::now();
    // Milliseconds since `started` of the last progress report.
    let last_change = AtomicU64::new(0);
    // The stall notice is armed once the package is on the device (or installd
    // reported progress): the upload before it has no progress callback.
    let armed = AtomicBool::new(false);
    let callback = |(percent, ()): (u64, ())| {
        // installd repeats a percentage across several status updates.
        if last.swap(percent, Ordering::Relaxed) != percent {
            last_change.store(started.elapsed().as_millis() as u64, Ordering::Relaxed);
            armed.store(true, Ordering::Relaxed);
            println!("PROGRESS {percent}");
            flush_stdout();
        }
        async {}
    };
    let install = async {
        if args.package.is_file() {
            let (remote, options) = upload_ipa(&mut connection, &args.package).await?;
            last_change.store(started.elapsed().as_millis() as u64, Ordering::Relaxed);
            armed.store(true, Ordering::Relaxed);
            println!("UPLOADED");
            flush_stdout();
            let mut client = InstallationProxyClient::connect_rsd(
                &mut connection.adapter,
                &mut connection.handshake,
            )
            .await?;
            client
                .install_with_callback(remote, Some(options), callback, ())
                .await?;
        } else {
            idevice::utils::installation::install_package_with_callback_rsd(
                &mut connection.adapter,
                &mut connection.handshake,
                &args.package,
                None,
                callback,
                (),
            )
            .await?;
        }
        anyhow::Ok(())
    };
    tokio::pin!(install);

    let mut ticker = tokio::time::interval(Duration::from_secs(5));
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let mut waiting_reported = 0u64;
    loop {
        tokio::select! {
            result = &mut install => {
                result?;
                break;
            }
            _ = ticker.tick() => {
                if args.stall == 0 || !armed.load(Ordering::Relaxed) {
                    continue;
                }
                let since = started.elapsed().as_millis() as u64
                    - last_change.load(Ordering::Relaxed);
                let stalled = since / 1000;
                // Report once per stall period while it lasts.
                if stalled >= args.stall && stalled / args.stall > waiting_reported {
                    waiting_reported = stalled / args.stall;
                    let percent = match last.load(Ordering::Relaxed) {
                        u64::MAX => 0,
                        p => p,
                    };
                    println!("WAITING {percent} {stalled}");
                    flush_stdout();
                } else if stalled < args.stall {
                    waiting_reported = 0;
                }
            }
        }
    }

    println!("INSTALLED");
    Ok(())
}

/// Uploads an .ipa to `PublicStaging` over AFC, as idevice's install helper does,
/// so the caller knows when the upload ends and installd's work begins.
/// Returns the remote path and the Install options.
async fn upload_ipa(
    connection: &mut RsdConnection,
    package: &std::path::Path,
) -> Result<(String, plist::Value)> {
    use idevice::afc::AfcClient;
    use idevice::afc::opcode::AfcFopenMode;

    let data = tokio::fs::read(package).await?;
    let bundle_id = ipa_bundle_id(&data)?;

    let mut afc = AfcClient::connect_rsd(&mut connection.adapter, &mut connection.handshake).await?;
    if afc.get_file_info("PublicStaging").await.is_err() {
        afc.mk_dir("PublicStaging").await?;
    }
    let remote = "PublicStaging/idevice.ipa".to_string();
    let mut file = afc.open(remote.clone(), AfcFopenMode::WrOnly).await?;
    file.write_entire(&data).await?;
    file.close().await?;

    let options = plist::Value::Dictionary(plist::Dictionary::from_iter([(
        "CFBundleIdentifier".to_string(),
        plist::Value::String(bundle_id),
    )]));
    Ok((remote, options))
}

/// `CFBundleIdentifier` of `Payload/<name>.app/Info.plist` inside an .ipa.
fn ipa_bundle_id(data: &[u8]) -> Result<String> {
    use std::io::Read;

    let mut archive = zip::ZipArchive::new(std::io::Cursor::new(data))?;
    let name = (0..archive.len())
        .filter_map(|i| archive.name_for_index(i).map(str::to_string))
        .find(|n| {
            let parts: Vec<_> = n.split('/').collect();
            parts.len() == 3
                && parts[0] == "Payload"
                && parts[1].ends_with(".app")
                && parts[2] == "Info.plist"
        });
    let Some(name) = name else {
        bail!("Not an .ipa: no Payload/*.app/Info.plist");
    };
    let mut raw = Vec::new();
    archive.by_name(&name)?.read_to_end(&mut raw)?;
    let info: plist::Dictionary = plist::from_bytes(&raw)?;
    match info.get("CFBundleIdentifier").and_then(|v| v.as_string()) {
        Some(id) => Ok(id.to_string()),
        None => bail!("No CFBundleIdentifier in {name}"),
    }
}

async fn run_apps(store: &PairingStore, host: &str, args: AppsArgs) -> Result<()> {
    let mut connection = connect(store, host, &args.target).await?;

    // tvOS doesn't expose the CoreDevice app service over this tunnel, but the
    // installation proxy shim used by `install` is always there.
    let mut client =
        InstallationProxyClient::connect_rsd(&mut connection.adapter, &mut connection.handshake)
            .await?;
    let apps = client
        .get_apps(Some("User"), args.bundle_id.clone().map(|id| vec![id]))
        .await?;

    let mut apps: Vec<_> = apps.into_iter().collect();
    apps.sort_by(|a, b| a.0.cmp(&b.0));

    for (bundle_id, info) in apps {
        let field = |key: &str| {
            info.as_dictionary()
                .and_then(|d| d.get(key))
                .and_then(plist_scalar)
        };
        let version = field("CFBundleShortVersionString");
        let build = field("CFBundleVersion");
        let name = field("CFBundleDisplayName").or_else(|| field("CFBundleName"));

        if args.json {
            println!(
                "{}",
                serde_json::json!({
                    "bundle_id": bundle_id,
                    "name": name,
                    "version": version,
                    "build": build,
                })
            );
        } else {
            println!(
                "{} {} {} {}",
                bundle_id,
                version.as_deref().unwrap_or("-"),
                build.as_deref().unwrap_or("-"),
                name.as_deref().unwrap_or("-")
            );
        }
    }

    Ok(())
}

/// An installed app, as far as matching it to a process goes.
struct InstalledApp {
    bundle_id: String,
    /// Bundle path on the device (`/private/var/containers/Bundle/Application/<UUID>/X.app`).
    path: Option<String>,
    executable: Option<String>,
    user: bool,
}

/// A process seen on the device.
struct RunningProcess {
    pid: u64,
    name: Option<String>,
    path: Option<String>,
    /// Bundle id of the app whose MAIN executable this is (None for extensions and daemons).
    bundle_id: Option<String>,
    /// `app` (main executable), `extension` (a helper inside an app bundle: Top Shelf, XPC...) or `other`.
    kind: &'static str,
    /// For an extension, the bundle id of the app that contains it.
    parent_bundle_id: Option<String>,
}

/// Largest os_trace PidList reply accepted (the device chooses the length), and how long to wait for it.
const OS_TRACE_MAX_REPLY: usize = 16 * 1024 * 1024;
const OS_TRACE_TIMEOUT: Duration = Duration::from_secs(30);

const APP_SERVICE: &str = "com.apple.coredevice.appservice";
const OS_TRACE_SERVICE: &str = "com.apple.os_trace_relay.shim.remote";

async fn installed_apps_for_matching(connection: &mut RsdConnection) -> Result<Vec<InstalledApp>> {
    let mut client =
        InstallationProxyClient::connect_rsd(&mut connection.adapter, &mut connection.handshake)
            .await?;
    let apps = client.get_apps(Some("Any"), None).await?;
    Ok(apps
        .into_iter()
        .map(|(bundle_id, info)| {
            let field = |key: &str| {
                info.as_dictionary()
                    .and_then(|d| d.get(key))
                    .and_then(|v| v.as_string())
                    .map(str::to_string)
            };
            InstalledApp {
                path: field("Path"),
                executable: field("CFBundleExecutable"),
                user: field("ApplicationType").as_deref() == Some("User"),
                bundle_id,
            }
        })
        .collect())
}

/// `/private/var/x` and `/var/x` name the same place; URLs may carry `file://`.
fn normalize_path(path: &str) -> String {
    let path = path.strip_prefix("file://").unwrap_or(path);
    let path = path.strip_prefix("/private").unwrap_or(path);
    path.trim_end_matches('/').to_string()
}

/// App whose bundle directory contains `executable`, and whether that executable is the app's own
/// main binary (`<bundle>/<CFBundleExecutable>`) rather than an extension or helper inside it.
fn classify_by_path<'a>(
    apps: &'a [InstalledApp],
    executable: &str,
) -> Option<(&'a InstalledApp, bool)> {
    let executable = normalize_path(executable);
    apps.iter().find_map(|app| {
        let bundle = normalize_path(app.path.as_deref()?);
        let relative = executable.strip_prefix(&(bundle + "/"))?;
        let is_main = match app.executable.as_deref() {
            Some(main) => relative == main,
            // Executable inconnu : on ne retient que ce qui est a la racine du bundle.
            None => !relative.contains('/'),
        };
        Some((app, is_main))
    })
}

/// The app whose MAIN executable is `executable`.
#[cfg(test)]
fn match_by_path<'a>(apps: &'a [InstalledApp], executable: &str) -> Option<&'a InstalledApp> {
    classify_by_path(apps, executable)
        .filter(|(_, is_main)| *is_main)
        .map(|(app, _)| app)
}

fn match_by_name<'a>(apps: &'a [InstalledApp], name: &str) -> Option<&'a InstalledApp> {
    // A user app's executable name can shadow a system one: prefer user apps.
    let matches = |app: &&InstalledApp| app.executable.as_deref() == Some(name);
    apps.iter()
        .filter(|a| a.user)
        .find(matches)
        .or_else(|| apps.iter().find(matches))
}

/// Process list through CoreDevice's app service when the device offers it,
/// otherwise through the os_trace relay (process names only). Read-only.
async fn list_processes(
    connection: &mut RsdConnection,
) -> Result<(Vec<RunningProcess>, &'static str)> {
    let mut names: Vec<_> = connection.handshake.services.keys().cloned().collect();
    names.sort();
    log::debug!("RSD services: {}", names.join(", "));

    let apps = installed_apps_for_matching(connection).await?;

    if connection.handshake.services.contains_key(APP_SERVICE) {
        use idevice::core_device::AppServiceClient;
        match AppServiceClient::connect_rsd(&mut connection.adapter, &mut connection.handshake)
            .await
        {
            Ok(mut client) => match client.list_processes().await {
                Ok(tokens) => {
                    let processes = tokens
                        .into_iter()
                        .map(|token| {
                            let path = token.executable_url.map(|u| u.relative);
                            let class = path.as_deref().and_then(|p| classify_by_path(&apps, p));
                            let name = path
                                .as_deref()
                                .and_then(|p| p.rsplit('/').next())
                                .map(str::to_string);
                            let (bundle_id, kind, parent_bundle_id) = match class {
                                Some((app, true)) => (Some(app.bundle_id.clone()), "app", None),
                                Some((app, false)) => {
                                    (None, "extension", Some(app.bundle_id.clone()))
                                }
                                None => (None, "other", None),
                            };
                            RunningProcess {
                                pid: token.pid as u64,
                                bundle_id,
                                name,
                                path: path.map(|p| normalize_path(&p)),
                                kind,
                                parent_bundle_id,
                            }
                        })
                        .collect();
                    return Ok((processes, "coredevice"));
                }
                Err(e) => log::warn!("CoreDevice process list failed, trying os_trace: {e}"),
            },
            Err(e) => log::warn!("CoreDevice app service unavailable, trying os_trace: {e}"),
        }
    }

    if connection.handshake.services.contains_key(OS_TRACE_SERVICE) {
        let pids = os_trace_pid_list(connection).await?;
        let processes = pids
            .into_iter()
            .map(|(pid, name)| {
                let app = name.as_deref().and_then(|n| match_by_name(&apps, n));
                RunningProcess {
                    pid,
                    bundle_id: app.map(|a| a.bundle_id.clone()),
                    path: app.and_then(|a| a.path.clone()),
                    kind: if app.is_some() { "app" } else { "other" },
                    parent_bundle_id: None,
                    name,
                }
            })
            .collect();
        return Ok((processes, "os_trace"));
    }

    Err(ProcessListUnsupported.into())
}

#[derive(Debug)]
struct ProcessListUnsupported;

impl std::fmt::Display for ProcessListUnsupported {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "the device offers no service that lists processes")
    }
}

impl std::error::Error for ProcessListUnsupported {}

/// `PidList` request of the os_trace relay, keeping the process names that
/// idevice's `get_pid_list` drops: `{ "Payload": { "<pid>": { "ProcessName": ... } } }`.
async fn os_trace_pid_list(connection: &mut RsdConnection) -> Result<Vec<(u64, Option<String>)>> {
    use idevice::os_trace_relay::OsTraceRelayClient;

    let mut client =
        OsTraceRelayClient::connect_rsd(&mut connection.adapter, &mut connection.handshake).await?;
    let mut request = Vec::new();
    plist::Value::Dictionary(plist::Dictionary::from_iter([(
        "Request".to_string(),
        plist::Value::String("PidList".into()),
    )]))
    .to_writer_binary(&mut request)?;
    let mut message = (request.len() as u32).to_be_bytes().to_vec();
    message.extend_from_slice(&request);
    client.idevice.send_raw(&message).await?;

    // One status byte (1 = success), then a length-prefixed plist. The device controls both: check
    // the status, cap the length, bound the wait, and report "unsupported" rather than aborting.
    let body = tokio::time::timeout(OS_TRACE_TIMEOUT, async {
        let status = client.idevice.read_raw(1).await?;
        if status.first() != Some(&1) {
            return Err(anyhow::Error::from(ProcessListUnsupported));
        }
        let len = client.idevice.read_raw(4).await?;
        let len = u32::from_be_bytes([len[0], len[1], len[2], len[3]]) as usize;
        if len == 0 || len > OS_TRACE_MAX_REPLY {
            return Err(anyhow::Error::from(ProcessListUnsupported));
        }
        Ok(client.idevice.read_raw(len).await?)
    })
    .await
    .map_err(|_| anyhow::Error::from(ProcessListUnsupported))??;
    let response: plist::Dictionary = plist::from_bytes(&body)?;

    let Some(payload) = response.get("Payload").and_then(|p| p.as_dictionary()) else {
        bail!("unexpected PidList response: no Payload");
    };
    let mut out: Vec<_> = payload
        .iter()
        .filter_map(|(pid, info)| {
            let pid = pid.parse::<u64>().ok()?;
            log::trace!("PidList {pid}: {info:?}");
            let name = info
                .as_dictionary()
                .and_then(|d| d.get("ProcessName"))
                .and_then(|n| n.as_string())
                .map(str::to_string);
            Some((pid, name))
        })
        .collect();
    out.sort_by_key(|(pid, _)| *pid);
    Ok(out)
}

async fn run_processes(store: &PairingStore, host: &str, args: ProcessesArgs) -> Result<()> {
    let mut connection = connect(store, host, &args.target).await?;
    let (processes, source) = list_processes(&mut connection).await?;
    log::info!("{} processes listed through {source}", processes.len());

    for process in processes {
        if !args.all && process.bundle_id.is_none() {
            continue;
        }
        if args.json {
            println!(
                "{}",
                serde_json::json!({
                    "pid": process.pid,
                    "name": process.name,
                    "bundle_id": process.bundle_id,
                    "path": process.path,
                    "kind": process.kind,
                    "parent_bundle_id": process.parent_bundle_id,
                    "source": source,
                })
            );
        } else {
            println!(
                "{} {} {}",
                process.pid,
                process.bundle_id.as_deref().unwrap_or("-"),
                process.name.as_deref().unwrap_or("-")
            );
        }
    }
    Ok(())
}

/// Exit code OK when the app has a main process on the device (extensions such as Top Shelf do not
/// count), NOT_RUNNING otherwise. A resident process is not proof the app is in the foreground.
async fn run_running(store: &PairingStore, host: &str, args: RunningArgs) -> Result<i32> {
    let mut connection = connect(store, host, &args.target).await?;
    let (processes, source) = list_processes(&mut connection).await?;
    let found = processes.iter().find(|p| {
        p.bundle_id
            .as_deref()
            .is_some_and(|b| b.eq_ignore_ascii_case(&args.bundle_id))
    });
    match found {
        Some(process) => {
            println!("RUNNING {} {} {source}", args.bundle_id, process.pid);
            Ok(exit_code::OK)
        }
        None => {
            println!("NOT_RUNNING {} {source}", args.bundle_id);
            Ok(exit_code::NOT_RUNNING)
        }
    }
}

/// Maps an error to its exit code and the keyword of its `ERROR` line.
fn classify(error: &anyhow::Error) -> (i32, &'static str) {
    use plume_utils::Error as E;

    for cause in error.chain() {
        if cause.downcast_ref::<ProcessListUnsupported>().is_some() {
            return (exit_code::UNSUPPORTED, "UNSUPPORTED");
        }
        if let Some(e) = cause.downcast_ref::<E>() {
            return match e {
                E::PairingNotAccepted | E::PairingNotFound(_) => {
                    (exit_code::PAIRING_LOST, "PAIRING_LOST")
                }
                E::DeviceNotFound(_) | E::Unreachable(_) | E::Idevice(IdeviceError::Socket(_)) => {
                    (exit_code::NOT_FOUND, "NOT_FOUND")
                }
                _ => (exit_code::OTHER, "FAILED"),
            };
        }
        if let Some(IdeviceError::Socket(_)) = cause.downcast_ref::<IdeviceError>() {
            return (exit_code::NOT_FOUND, "NOT_FOUND");
        }
    }

    (exit_code::OTHER, "FAILED")
}

/// Ends the process with a stable exit code. Failures also print one
/// `ERROR <KIND> <message>` line on stdout for callers reading line by line.
fn exit_with(result: Result<()>) -> ! {
    exit_with_code(result.map(|()| exit_code::OK))
}

/// Like `exit_with`, for commands whose success carries its own exit code.
fn exit_with_code(result: Result<i32>) -> ! {
    let code = match result {
        Ok(code) => code,
        Err(e) => {
            let (code, kind) = classify(&e);
            log::error!("{e:#}");
            println!(
                "ERROR {kind} {}",
                format!("{e:#}").replace(['\r', '\n'], " ")
            );
            code
        }
    };
    flush_stdout();
    std::process::exit(code)
}

fn flush_stdout() {
    let _ = std::io::stdout().flush();
}

async fn run_discover(store: &PairingStore, args: DiscoverArgs) -> Result<()> {
    let timeout = Duration::from_secs(args.timeout);
    let devices = match args.ip {
        Some(ip) => discover_at(Hint::at(ip.socket_addr(args.mdns_port)), timeout).await?,
        None => discover(timeout).await?,
    };

    if devices.is_empty() {
        log::warn!("No devices found. Check that the device is awake and on this network.");
        return Ok(());
    }

    for device in devices {
        let known = match (&device.identifier, &device.auth_tag) {
            (Some(identifier), Some(auth_tag)) => store.find(identifier, auth_tag).await?,
            _ => None,
        };

        if args.json {
            println!(
                "{}",
                serde_json::json!({
                    "kind": device.kind.to_string(),
                    "name": device.display_name(),
                    "hostname": device.hostname,
                    "identifier": device.identifier,
                    "auth_tag": device.auth_tag,
                    "ip": format_scoped(&device.address),
                    "port": device.address.port(),
                    "udid": known.as_ref().map(|record| record.udid.clone()),
                })
            );
            continue;
        }

        match (device.kind, known) {
            (_, Some(record)) => println!("{device} paired as {}", record.udid),
            (ServiceKind::ManualPairing, None) => println!(
                "{device} run: {} pair --ip {} --port {}",
                invocation(),
                format_scoped(&device.address),
                device.address.port()
            ),
            (ServiceKind::RemotePairing, None) => println!("{device} paired with another host"),
        }
    }

    Ok(())
}

async fn run_list(store: &PairingStore) -> Result<()> {
    let records = store.list().await?;

    if records.is_empty() {
        log::warn!("No paired devices in {}", store.directory().display());
        return Ok(());
    }

    for record in records {
        println!("{record}");
    }

    Ok(())
}

async fn run_find(store: &PairingStore, args: FindArgs) -> Result<()> {
    match store.find(&args.identifier, &args.auth_tag).await? {
        Some(record) => {
            println!("{record}");
            Ok(())
        }
        None => bail!("No stored pairing matches identifier {}", args.identifier),
    }
}

async fn run_forget(store: &PairingStore, args: ForgetArgs) -> Result<()> {
    if store.remove(&args.udid).await? {
        log::info!("Forgot pairing for {}", args.udid);
        Ok(())
    } else {
        bail!("No stored pairing for {}", args.udid)
    }
}

fn invocation() -> String {
    std::env::args()
        .next()
        .filter(|arg| !arg.is_empty())
        .unwrap_or_else(|| "plumesign".to_string())
}

/// Plain stdin read rather than a prompt widget: this is often driven by another
/// process writing the PIN to the child's stdin, where there is no terminal.
///
/// `PIN_REQUIRED` goes to stdout on its own line so a parent reading line by
/// line sees it before this blocks; the human prompt goes to stderr.
fn read_pin() -> String {
    println!("PIN_REQUIRED");
    flush_stdout();
    eprintln!("Enter the PIN shown on the device:");

    // Lecture dans un thread : sans reponse (le parent n'ecrit jamais), on abandonne au bout de
    // PIN_TIMEOUT au lieu de bloquer indefiniment.
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut line = String::new();
        let read = std::io::stdin().lock().read_line(&mut line);
        let _ = tx.send((read, line));
    });

    match rx.recv_timeout(PIN_TIMEOUT) {
        // 0 octet lu = EOF : stdin ferme avant la saisie.
        Ok((Ok(0), _)) => cancel_pin("stdin closed before a PIN was given"),
        Ok((Ok(_), line)) => {
            normalize_pin(&line).unwrap_or_else(|| cancel_pin("empty PIN"))
        }
        Ok((Err(e), _)) => cancel_pin(&format!("cannot read the PIN: {e}")),
        Err(_) => cancel_pin("timed out waiting for the PIN"),
    }
}

/// Le PIN affiche par la TV, sans espaces ; `None` s'il est vide.
fn normalize_pin(raw: &str) -> Option<String> {
    let pin = raw.trim();
    (!pin.is_empty()).then(|| pin.to_string())
}

/// Un PIN vide envoye a la TV compte comme un essai rate (et peut la verrouiller) : on s'arrete
/// avant qu'un seul octet ne parte, avec une ligne `ERROR CANCELLED` pour l'appelant.
fn cancel_pin(reason: &str) -> ! {
    println!("ERROR CANCELLED PIN not provided: {reason}");
    flush_stdout();
    std::process::exit(exit_code::CANCELLED)
}

/// Duree pendant laquelle la TV affiche son PIN.
const PIN_TIMEOUT: Duration = Duration::from_secs(120);

#[cfg(test)]
mod tests {
    use super::*;

    fn ipa(entries: &[(&str, &[u8])]) -> Vec<u8> {
        use std::io::Write;
        let mut out = std::io::Cursor::new(Vec::new());
        let mut zip = zip::ZipWriter::new(&mut out);
        for (name, data) in entries {
            zip.start_file(*name, zip::write::SimpleFileOptions::default())
                .unwrap();
            zip.write_all(data).unwrap();
        }
        zip.finish().unwrap();
        out.into_inner()
    }

    #[test]
    fn bundle_id_from_ipa() {
        let mut info = Vec::new();
        plist::Value::Dictionary(plist::Dictionary::from_iter([(
            "CFBundleIdentifier".to_string(),
            plist::Value::String("com.example.tv".into()),
        )]))
        .to_writer_xml(&mut info)
        .unwrap();
        let data = ipa(&[
            ("Payload/X.app/Frameworks/F.framework/Info.plist", b"nope"),
            ("Payload/X.app/Info.plist", &info),
        ]);
        assert_eq!(ipa_bundle_id(&data).unwrap(), "com.example.tv");
        assert!(ipa_bundle_id(&ipa(&[("Payload/readme.txt", b"x")])).is_err());
    }

    #[test]
    fn family_comes_from_the_class_then_the_model() {
        assert_eq!(device_family(Some("AppleTV"), None), Some("tvos"));
        assert_eq!(device_family(None, Some("AppleTV11,1")), Some("tvos"));
        assert_eq!(device_family(Some("iPhone"), None), Some("ios"));
        assert_eq!(device_family(None, Some("iPhone15,2")), Some("ios"));
        assert_eq!(device_family(None, Some("iPod9,1")), Some("ios"));
        assert_eq!(device_family(Some("iPad"), Some("iPad13,4")), Some("ipados"));
        assert_eq!(device_family(None, Some("RealityDevice14,1")), Some("visionos"));
        assert_eq!(device_family(Some("Watch"), Some("Watch6,1")), None);
        assert_eq!(device_family(None, None), None);
    }

    #[test]
    fn import_stores_a_valid_pairing_file_and_refuses_garbage() {
        let dir = std::env::temp_dir().join(format!(
            "plumesign-import-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let good = dir.join("good.plist");
        std::fs::write(&good, RpPairingFile::generate("tvsign").to_bytes()).unwrap();
        let bad = dir.join("bad.plist");
        std::fs::write(&bad, b"not a plist").unwrap();

        let store = PairingStore::new(dir.join("store"));
        let args = |file: &std::path::Path| ImportArgs {
            file: file.to_path_buf(),
            udid: "00008110-001A2B3C00000000".to_string(),
            name: Some("Mon iPhone".to_string()),
            model: Some("iPhone15,2".to_string()),
        };
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        rt.block_on(async {
            assert!(run_import(&store, "tvsign", args(&bad)).await.is_err());
            assert!(store.list().await.unwrap().is_empty());
            run_import(&store, "tvsign", args(&good)).await.unwrap();
            let records = store.list().await.unwrap();
            assert_eq!(records.len(), 1);
            assert_eq!(records[0].name, "Mon iPhone");
            assert_eq!(records[0].model, "iPhone15,2");
            assert_eq!(records[0].udid, "00008110-001A2B3C00000000");
        });
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn paths_match_with_or_without_private() {
        let apps = vec![InstalledApp {
            bundle_id: "com.example.tv".into(),
            path: Some("/private/var/containers/Bundle/Application/U/X.app".into()),
            executable: Some("X".into()),
            user: true,
        }];
        assert!(match_by_path(&apps, "file:///var/containers/Bundle/Application/U/X.app/X").is_some());
        assert!(match_by_path(&apps, "/private/var/containers/Bundle/Application/U/X.apps/X").is_none());
        assert!(match_by_name(&apps, "X").is_some());
        assert!(match_by_name(&apps, "Y").is_none());
    }

    #[test]
    fn extensions_do_not_count_as_the_app() {
        let apps = vec![InstalledApp {
            bundle_id: "com.example.tv".into(),
            path: Some("/private/var/containers/Bundle/Application/U/X.app".into()),
            executable: Some("X".into()),
            user: true,
        }];
        let base = "/private/var/containers/Bundle/Application/U/X.app";
        // Le binaire principal.
        assert!(match_by_path(&apps, &format!("{base}/X")).is_some());
        // Top Shelf et autres extensions : rattaches a l'app, jamais pris pour elle.
        let top_shelf = format!("{base}/PlugIns/TopShelf.appex/TopShelf");
        assert!(match_by_path(&apps, &top_shelf).is_none());
        let (app, is_main) = classify_by_path(&apps, &top_shelf).unwrap();
        assert_eq!(app.bundle_id, "com.example.tv");
        assert!(!is_main);
        assert!(match_by_path(&apps, &format!("{base}/XPCServices/H.xpc/H")).is_none());
        // Autre binaire a la racine du bundle (assistant) : pas l'app non plus.
        assert!(match_by_path(&apps, &format!("{base}/helper")).is_none());
    }

    #[test]
    fn executable_unknown_only_matches_the_bundle_root() {
        let apps = vec![InstalledApp {
            bundle_id: "com.example.tv".into(),
            path: Some("/var/containers/Bundle/Application/U/X.app".into()),
            executable: None,
            user: true,
        }];
        assert!(match_by_path(&apps, "/var/containers/Bundle/Application/U/X.app/X").is_some());
        assert!(
            match_by_path(&apps, "/var/containers/Bundle/Application/U/X.app/PlugIns/T.appex/T")
                .is_none()
        );
    }

    #[test]
    fn scoped_ip_parsing() {
        let plain = parse_scoped_ip("192.168.1.5").unwrap();
        assert_eq!(plain.scope_id, 0);
        assert_eq!(plain.socket_addr(49152).to_string(), "192.168.1.5:49152");

        let scoped = parse_scoped_ip("fe80::1%12").unwrap();
        assert_eq!(scoped.scope_id, 12);
        match scoped.socket_addr(49152) {
            SocketAddr::V6(v6) => {
                assert_eq!(v6.scope_id(), 12);
                assert_eq!(v6.port(), 49152);
            }
            _ => panic!("expected IPv6"),
        }

        assert!(parse_scoped_ip("fe80::1%eth0").is_err());
        assert!(parse_scoped_ip("192.168.1.5%12").is_err());
        assert!(parse_scoped_ip("nope").is_err());
        assert_eq!(parse_scoped_ip("2001:db8::1").unwrap().scope_id, 0);
    }

    #[test]
    fn discover_output_keeps_the_scope_id() {
        let v6 = SocketAddr::V6(SocketAddrV6::new("fe80::1".parse().unwrap(), 49152, 0, 12));
        assert_eq!(format_scoped(&v6), "fe80::1%12");
        let global: SocketAddr = "[2001:db8::1]:49152".parse().unwrap();
        assert_eq!(format_scoped(&global), "2001:db8::1");
        let v4: SocketAddr = "192.168.1.5:49152".parse().unwrap();
        assert_eq!(format_scoped(&v4), "192.168.1.5");
        // Ce que discover imprime se relit tel quel avec --ip.
        let back = parse_scoped_ip(&format_scoped(&v6)).unwrap();
        assert_eq!(back.socket_addr(49152), v6);
    }

    #[test]
    fn an_empty_pin_is_never_sent() {
        assert_eq!(normalize_pin(" 1234\n"), Some("1234".to_string()));
        assert_eq!(normalize_pin("   \n"), None);
        assert_eq!(normalize_pin(""), None);
    }
}
