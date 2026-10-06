use std::io::{BufRead, Write};
use std::net::{IpAddr, SocketAddr};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use anyhow::{Result, bail};
use clap::{Args, Subcommand};
use idevice::IdeviceError;
use idevice::RsdService;
use idevice::installation_proxy::InstallationProxyClient;
use plume_utils::wireless::{
    PairingStore, RsdConnection, ServiceKind, discover, locate, locate_and_connect, pair,
};

use crate::get_data_path;

const DEFAULT_HOST: &str = "plume";

#[derive(Debug, Args)]
pub struct PairArgs {
    #[command(subcommand)]
    pub command: Option<PairCommands>,
    /// IP address of the device to pair with
    #[arg(long = "ip", value_name = "IP", requires = "port")]
    pub ip: Option<IpAddr>,
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
    /// Print a paired device's properties over the wireless tunnel
    Info(InfoArgs),
    /// Check that a paired device is reachable and still trusts this host
    Check(TargetArgs),
    /// Install an app on a paired device over the wireless tunnel
    Install(InstallArgs),
    /// List the user apps installed on a paired device
    Apps(AppsArgs),
}

/// Exit codes for the wireless commands (`info`, `check`, `install`, `apps`).
pub mod exit_code {
    pub const OK: i32 = 0;
    pub const OTHER: i32 = 1;
    /// The pairing is missing, or the device no longer accepts it.
    pub const PAIRING_LOST: i32 = 2;
    /// No matching device on the network, or it stopped responding.
    pub const NOT_FOUND: i32 = 3;
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
        None => run_pair(&store, args).await,
    }
}

async fn run_pair(store: &PairingStore, args: PairArgs) -> Result<()> {
    let (Some(ip), Some(port)) = (args.ip, args.port) else {
        bail!("--ip and --port are required to pair; run `plumesign pair discover` to find them");
    };
    let address = SocketAddr::new(ip, port);

    log::info!("Pairing with {address} as {:?}", args.host);

    let (pairing_file, device) = pair(address, &args.host, || {
        let pin = args.pin.clone();
        async move {
            match pin {
                Some(pin) => pin,
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
    let identifier = match locate(store, &device.udid, Duration::from_secs(5)).await {
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

async fn connect(store: &PairingStore, host: &str, target: &TargetArgs) -> Result<RsdConnection> {
    let connection = locate_and_connect(
        store,
        &target.identifier,
        host,
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
    idevice::utils::installation::install_package_with_callback_rsd(
        &mut connection.adapter,
        &mut connection.handshake,
        &args.package,
        None,
        |(percent, ())| {
            // installd repeats a percentage across several status updates.
            if last.swap(percent, Ordering::Relaxed) != percent {
                println!("PROGRESS {percent}");
                flush_stdout();
            }
            async {}
        },
        (),
    )
    .await?;

    println!("INSTALLED");
    Ok(())
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

/// Maps an error to its exit code and the keyword of its `ERROR` line.
fn classify(error: &anyhow::Error) -> (i32, &'static str) {
    use plume_utils::Error as E;

    for cause in error.chain() {
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
    let code = match result {
        Ok(()) => exit_code::OK,
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
    let devices = discover(Duration::from_secs(args.timeout)).await?;

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
                    "ip": device.address.ip().to_string(),
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
                device.address.ip(),
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

    let mut line = String::new();
    if std::io::stdin().lock().read_line(&mut line).is_err() {
        return String::new();
    }

    line.trim().to_string()
}
