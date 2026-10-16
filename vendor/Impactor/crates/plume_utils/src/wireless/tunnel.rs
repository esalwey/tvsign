//! Wireless RSD access to a device we already paired with.
//!
//! The whole chain runs in userspace, with no TUN interface or admin rights:
//! mDNS `_remotepairing._tcp` -> pair-verify with the stored `RpPairingFile` ->
//! `createListener` -> TLS-PSK tunnel -> jktcp TCP stack -> RSD handshake.

use std::net::SocketAddr;
use std::time::Duration;

use idevice::IdeviceError;
use idevice::remote_pairing::errors::RemotePairingError;
use idevice::remote_pairing::{
    RemotePairingClient, RpPairingFile, RpPairingSocket, connect_tls_psk_tunnel_native,
};
use idevice::rsd::RsdHandshake;
use idevice::tcp::adapter::Adapter;
use idevice::tcp::handle::AdapterHandle;
use mdns_sd::{ServiceDaemon, ServiceEvent};

use super::discovery::{DiscoveredDevice, ServiceKind};
use super::store::{PairingRecord, PairingStore};
use super::unicast::{DIRECT_TIMEOUT, Hint, query};
use crate::Error;

/// How long a single network step may take before the device counts as unreachable.
const STEP_TIMEOUT: Duration = Duration::from_secs(15);

/// A paired device seen on the network whose `authTag` proves it is ours.
#[derive(Debug, Clone)]
pub struct LocatedDevice {
    pub record: PairingRecord,
    /// `identifier` TXT record of the device's `_remotepairing._tcp` service.
    pub service_identifier: String,
    pub address: SocketAddr,
}

/// An open tunnel to a device, ready for RSD services.
pub struct RsdConnection {
    pub device: LocatedDevice,
    pub adapter: AdapterHandle,
    pub handshake: RsdHandshake,
}

/// What we are looking for, and what a located candidate is judged against.
struct Wanted {
    identifier: String,
    udid: Option<String>,
    name: Option<String>,
    /// mDNS identifier remembered the last time the device was seen: lets its service be recognised
    /// even when the caller passes the pairing UDID and the tag no longer matches.
    stored_identifier: Option<String>,
}

/// What a single announcement means for the search.
enum Judged {
    /// One of our paired devices, and the one asked for.
    Found(LocatedDevice),
    /// The device asked for, but none of our keys produce its tag: it was reset or forgot this host.
    Refused,
    /// Not the device we want.
    Skip,
}

/// Judges a `_remotepairing._tcp` announcement against our pairings. A service whose tag none of our
/// keys produce (another host, or a device that forgot ours) is added to `foreign`; once the device
/// was never matched, `judge_foreign` decides whether those prove the pairing lost.
async fn judge(
    store: &PairingStore,
    wanted: &Wanted,
    device: &DiscoveredDevice,
    foreign: &mut Vec<ForeignService>,
) -> Result<Judged, Error> {
    let (Some(service_identifier), Some(auth_tag)) = (&device.identifier, &device.auth_tag) else {
        return Ok(Judged::Skip);
    };

    let mentioned = service_identifier.eq_ignore_ascii_case(&wanted.identifier)
        || wanted
            .stored_identifier
            .as_deref()
            .is_some_and(|stored| stored.eq_ignore_ascii_case(service_identifier));

    Ok(match store.find(service_identifier, auth_tag).await? {
        Some(record)
            if mentioned
                || wanted
                    .udid
                    .as_deref()
                    .is_some_and(|udid| udid.eq_ignore_ascii_case(&record.udid)) =>
        {
            log::debug!(
                "{} is {} at {}",
                service_identifier,
                record.udid,
                device.address
            );
            if wanted.stored_identifier.as_deref() != Some(service_identifier.as_str()) {
                if let Err(e) = store
                    .remember_identifier(&record.udid, service_identifier)
                    .await
                {
                    log::debug!("could not remember the advertised identifier: {e}");
                }
            }
            Judged::Found(LocatedDevice {
                record,
                service_identifier: service_identifier.clone(),
                address: device.address,
            })
        }
        // The device we were asked for, but none of our keys produce its
        // tag: it was reset or forgot this host.
        None if mentioned => Judged::Refused,
        None => {
            foreign.push(ForeignService {
                identifier: service_identifier.clone(),
                name: device.name.clone(),
            });
            Judged::Skip
        }
        _ => Judged::Skip,
    })
}

/// Finds a paired device by stored UDID or by its mDNS `identifier`.
///
/// Only `_remotepairing._tcp` is browsed; the manual pairing service, which
/// shows a PIN on screen when connected to, is never looked at. A device is
/// only returned once its `authTag` validates against one of our pairings, so
/// devices paired with other hosts are never targeted.
pub async fn locate(
    store: &PairingStore,
    identifier: &str,
    timeout: Duration,
) -> Result<LocatedDevice, Error> {
    locate_via(store, identifier, None, timeout).await
}

/// [`locate`], first asking `hint` directly (a unicast mDNS query) when given. That finds a device
/// whose multicast announcements do not reach us (VLAN, Docker bridge network, filtering access
/// point, firewall); when the direct query finds nothing usable the multicast browse still runs.
pub async fn locate_via(
    store: &PairingStore,
    identifier: &str,
    hint: Option<Hint>,
    timeout: Duration,
) -> Result<LocatedDevice, Error> {
    let records = store.list().await?;
    if records.is_empty() {
        return Err(Error::PairingNotFound(identifier.to_string()));
    }

    let known = records
        .iter()
        .find(|record| record.udid.eq_ignore_ascii_case(identifier));
    let wanted = Wanted {
        identifier: identifier.to_string(),
        udid: known.map(|record| record.udid.clone()),
        name: known.map(|record| record.name.clone()),
        stored_identifier: known.and_then(|record| record.service_identifier.clone()),
    };
    // Services whose tag matches none of our keys (another host, or a device that forgot ours).
    let mut foreign: Vec<ForeignService> = Vec::new();

    if let Some(hint) = hint {
        match query(hint, ServiceKind::RemotePairing, DIRECT_TIMEOUT).await {
            Ok(devices) => {
                for device in &devices {
                    match judge(store, &wanted, device, &mut foreign).await? {
                        Judged::Found(located) => return Ok(located),
                        Judged::Refused => return Err(Error::PairingNotAccepted),
                        Judged::Skip => {}
                    }
                }
                log::debug!("{} did not announce the device directly", hint.addr);
            }
            Err(e) => log::debug!("direct query to {} failed: {e}", hint.addr),
        }
    }

    let daemon = ServiceDaemon::new()?;
    let browse = daemon.browse(ServiceKind::RemotePairing.service_type())?;
    let deadline = tokio::time::Instant::now() + timeout;
    let mut result = Err(Error::DeviceNotFound(identifier.to_string()));

    loop {
        let event = tokio::select! {
            _ = tokio::time::sleep_until(deadline) => break,
            event = browse.recv_async() => event,
        };
        let Ok(event) = event else { break };

        let ServiceEvent::ServiceResolved(service) = event else {
            continue;
        };
        let Some(device) = DiscoveredDevice::from_resolved(ServiceKind::RemotePairing, &service)
        else {
            continue;
        };

        match judge(store, &wanted, &device, &mut foreign).await? {
            Judged::Found(located) => {
                result = Ok(located);
                break;
            }
            Judged::Refused => {
                result = Err(Error::PairingNotAccepted);
                break;
            }
            Judged::Skip => {}
        }
    }

    if matches!(result, Err(Error::DeviceNotFound(_))) {
        match judge_foreign(wanted.name.as_deref(), &foreign) {
            Verdict::Lost(name) => {
                log::warn!(
                    "the only device named {name:?} on the network no longer accepts this host's \
                     keys: treating the pairing as lost (if it is another device with the same \
                     name, pair this one again anyway)"
                );
                result = Err(Error::PairingNotAccepted);
            }
            Verdict::Ambiguous(name) => {
                log::warn!(
                    "several devices named {name:?} are on the network and none is paired with \
                     this host; not treating the pairing as lost"
                );
                result = Err(Error::DeviceNotFound(format!(
                    "{identifier} (other devices named {name:?} are visible but not paired with this host)"
                )));
            }
            Verdict::Nothing => {}
        }
    }

    if let Err(e) = daemon.shutdown() {
        log::debug!("failed to shut down mDNS daemon: {e}");
    }

    result
}

/// A `_remotepairing._tcp` service whose `authTag` none of our keys produce.
#[derive(Debug, Clone)]
struct ForeignService {
    identifier: String,
    name: Option<String>,
}

#[derive(Debug, PartialEq, Eq)]
enum Verdict {
    /// Exactly one distinct device carries our device's name and rejects our keys: it forgot this host.
    Lost(String),
    /// Several distinct devices carry that name: impossible to know which one is ours.
    Ambiguous(String),
    Nothing,
}

/// Decides, once the device was never matched, whether the unmatched services prove the pairing lost.
/// Le nom est librement modifiable (« Apple TV » par defaut), donc seul un nom porte par un seul
/// identifiant distinct est retenu ; plusieurs identifiants donnent un simple indice.
fn judge_foreign(wanted_name: Option<&str>, foreign: &[ForeignService]) -> Verdict {
    let Some(wanted) = wanted_name else {
        return Verdict::Nothing;
    };
    let mut ids: Vec<String> = Vec::new();
    let mut shown = None;
    for service in foreign {
        let Some(name) = service.name.as_deref() else {
            continue;
        };
        if !wanted.eq_ignore_ascii_case(name) {
            continue;
        }
        let id = service.identifier.to_ascii_lowercase();
        if !ids.contains(&id) {
            ids.push(id);
        }
        shown.get_or_insert_with(|| name.to_string());
    }
    match (ids.len(), shown) {
        (1, Some(name)) => Verdict::Lost(name),
        (n, Some(name)) if n > 1 => Verdict::Ambiguous(name),
        _ => Verdict::Nothing,
    }
}

/// Pair-verifies with `device` and opens a tunnel to its RSD service.
///
/// Never falls back to pairing: if the device no longer accepts our keys this
/// returns [`Error::PairingNotAccepted`] without a PIN being shown on screen.
pub async fn connect_rsd(
    store: &PairingStore,
    device: LocatedDevice,
    host: &str,
) -> Result<RsdConnection, Error> {
    let mut pairing_file = store.load(&device.record.udid).await?;
    let (adapter, handshake) = open_tunnel(device.address, host, &mut pairing_file).await?;

    Ok(RsdConnection {
        device,
        adapter,
        handshake,
    })
}

/// [`locate`] then [`connect_rsd`].
pub async fn locate_and_connect(
    store: &PairingStore,
    identifier: &str,
    host: &str,
    timeout: Duration,
) -> Result<RsdConnection, Error> {
    locate_and_connect_via(store, identifier, host, None, timeout).await
}

/// [`locate_via`] then [`connect_rsd`].
pub async fn locate_and_connect_via(
    store: &PairingStore,
    identifier: &str,
    host: &str,
    hint: Option<Hint>,
    timeout: Duration,
) -> Result<RsdConnection, Error> {
    let device = locate_via(store, identifier, hint, timeout).await?;
    connect_rsd(store, device, host).await
}

async fn open_tunnel(
    address: SocketAddr,
    host: &str,
    pairing_file: &mut RpPairingFile,
) -> Result<(AdapterHandle, RsdHandshake), Error> {
    let stream = step(tokio::net::TcpStream::connect(address))
        .await?
        .map_err(Error::Unreachable)?;
    let mut client = RemotePairingClient::new(RpPairingSocket::new(stream), host, pairing_file);

    step(client.attempt_pair_verify()).await??;
    if let Err(e) = step(client.validate_pairing()).await? {
        return Err(classify_verify_error(e));
    }

    let tunnel_port = step(client.create_tcp_listener()).await??;
    let tunnel_stream = step(tokio::net::TcpStream::connect(tunnel_address(
        address,
        tunnel_port,
    )))
    .await?
    .map_err(Error::Unreachable)?;
    let tunnel = step(connect_tls_psk_tunnel_native(
        Box::new(tunnel_stream),
        client.encryption_key(),
    ))
    .await??;

    let client_ip = tunnel
        .info
        .client_address
        .parse()
        .map_err(|e| Error::Other(format!("bad tunnel client address: {e}")))?;
    let server_ip = tunnel
        .info
        .server_address
        .parse()
        .map_err(|e| Error::Other(format!("bad tunnel server address: {e}")))?;
    let mtu = tunnel.info.mtu as usize;
    let rsd_port = tunnel.info.server_rsd_port;

    let mut adapter = Adapter::new(Box::new(tunnel.into_inner()), client_ip, server_ip);
    adapter.set_mss(mtu.saturating_sub(60));
    let mut adapter = adapter.to_async_handle();

    let rsd_stream = step(adapter.connect(rsd_port))
        .await?
        .map_err(Error::Unreachable)?;
    let handshake = step(RsdHandshake::new(rsd_stream)).await??;

    Ok((adapter, handshake))
}

/// Adresse du tunnel : celle de l'appareil avec un autre port. On part de `address` elle-meme pour que
/// l'identifiant de portee d'une adresse IPv6 link-local (fe80::...) ne soit pas perdu.
fn tunnel_address(address: SocketAddr, port: u16) -> SocketAddr {
    let mut tunnel = address;
    tunnel.set_port(port);
    tunnel
}

/// Seul un refus explicite de l'appareil prouve que l'appairage est perdu. Une reponse inattendue
/// (appareil qui se reveille, TLV tronque...) est une panne passagere, a reessayer plus tard.
fn classify_verify_error(error: IdeviceError) -> Error {
    match error {
        IdeviceError::Socket(io) => Error::Unreachable(io),
        IdeviceError::RemotePairing(
            RemotePairingError::PairVerifyFailed | RemotePairingError::PairingRejected(_),
        ) => {
            log::debug!("pair-verify rejected by the device");
            Error::PairingNotAccepted
        }
        other => {
            log::debug!("pair-verify failed without a clear rejection: {other}");
            Error::Unreachable(std::io::Error::new(
                std::io::ErrorKind::Other,
                format!("pair-verify answer not understood: {other}"),
            ))
        }
    }
}

async fn step<F: std::future::Future>(future: F) -> Result<F::Output, Error> {
    tokio::time::timeout(STEP_TIMEOUT, future)
        .await
        .map_err(|_| {
            Error::Unreachable(std::io::Error::new(
                std::io::ErrorKind::TimedOut,
                "device stopped responding",
            ))
        })
}

#[cfg(test)]
mod tests {
    use std::net::{IpAddr, Ipv6Addr, SocketAddrV6};

    use idevice::remote_pairing::RpPairingFile;
    use tokio::net::UdpSocket;

    use super::super::pairing::PairedDevice;
    use super::super::unicast::tests::response_with;
    use super::*;

    #[test]
    fn tunnel_address_keeps_the_ipv6_scope_id() {
        let ll = SocketAddr::V6(SocketAddrV6::new(
            Ipv6Addr::new(0xfe80, 0, 0, 0, 1, 2, 3, 4),
            49152,
            0,
            7,
        ));
        let tunnel = tunnel_address(ll, 50000);
        match tunnel {
            SocketAddr::V6(v6) => {
                assert_eq!(v6.scope_id(), 7);
                assert_eq!(v6.port(), 50000);
            }
            _ => panic!("expected an IPv6 address"),
        }
    }

    #[test]
    fn tunnel_address_replaces_the_port_for_ipv4() {
        let v4: SocketAddr = "192.168.1.20:49152".parse().unwrap();
        assert_eq!(tunnel_address(v4, 1234).to_string(), "192.168.1.20:1234");
    }

    fn foreign(identifier: &str, name: Option<&str>) -> ForeignService {
        ForeignService {
            identifier: identifier.into(),
            name: name.map(str::to_string),
        }
    }

    #[test]
    fn a_single_same_name_device_rejecting_our_keys_means_pairing_lost() {
        let seen = [
            foreign("AAAA", Some("Salon")),
            // The same service announced again (several interfaces) is still one device.
            foreign("aaaa", Some("salon")),
            foreign("BBBB", Some("Chambre")),
        ];
        assert_eq!(
            judge_foreign(Some("Salon"), &seen),
            Verdict::Lost("Salon".into())
        );
    }

    #[test]
    fn two_distinct_same_name_devices_are_only_a_hint() {
        let seen = [
            foreign("AAAA", Some("Apple TV")),
            foreign("BBBB", Some("Apple TV")),
        ];
        assert_eq!(
            judge_foreign(Some("Apple TV"), &seen),
            Verdict::Ambiguous("Apple TV".into())
        );
    }

    #[test]
    fn no_same_name_device_or_unknown_name_is_not_a_verdict() {
        let seen = [foreign("BBBB", Some("Chambre")), foreign("CCCC", None)];
        assert_eq!(judge_foreign(Some("Salon"), &seen), Verdict::Nothing);
        assert_eq!(judge_foreign(None, &seen), Verdict::Nothing);
        assert_eq!(judge_foreign(Some("Salon"), &[]), Verdict::Nothing);
    }

    #[test]
    fn only_an_explicit_rejection_means_pairing_lost() {
        assert!(matches!(
            classify_verify_error(RemotePairingError::PairVerifyFailed.into()),
            Error::PairingNotAccepted
        ));
        assert!(matches!(
            classify_verify_error(RemotePairingError::PairingRejected("no".into()).into()),
            Error::PairingNotAccepted
        ));
    }

    #[test]
    fn unexpected_answers_are_retryable_not_pairing_lost() {
        assert!(matches!(
            classify_verify_error(IdeviceError::UnexpectedResponse("x".into())),
            Error::Unreachable(_)
        ));
        assert!(matches!(
            classify_verify_error(IdeviceError::NotEnoughBytes(32, 3)),
            Error::Unreachable(_)
        ));
        let io = std::io::Error::new(std::io::ErrorKind::ConnectionReset, "reset");
        assert!(matches!(
            classify_verify_error(IdeviceError::Socket(io)),
            Error::Unreachable(_)
        ));
    }

    // Shared with idevice's own validate_auth_tag test (see store.rs).
    const ALT_IRK: [u8; 16] = [
        0x32, 0x0a, 0x7a, 0x64, 0x63, 0xf3, 0x5c, 0xcd, 0xa4, 0xbb, 0xd6, 0xeb, 0xe3, 0xab, 0xec,
        0x8b,
    ];
    const IDENTIFIER: &str = "2BE6E510-0325-4365-923E-B14C6F57DB3A";
    const AUTH_TAG: &str = "kXjlTr2l";
    const UDID: &str = "00008110-001A2B3C00000000";

    async fn store_with_pairing() -> PairingStore {
        let dir = std::env::temp_dir().join(format!("plume-locate-test-{}", uuid::Uuid::new_v4()));
        let store = PairingStore::new(dir);
        let mut file = RpPairingFile::generate("plume");
        file.alt_irk = Some(ALT_IRK.to_vec());
        let device = PairedDevice {
            udid: UDID.to_string(),
            name: "Salon".to_string(),
            model: "AppleTV11,1".to_string(),
            account_id: "plume".to_string(),
        };
        store.save(&device, &file).await.unwrap();
        store
    }

    /// A device that only answers direct queries is located through the hint, without multicast.
    #[tokio::test]
    async fn locate_via_finds_a_device_that_answers_directly() {
        let store = store_with_pairing().await;
        let server = UdpSocket::bind("127.0.0.1:0").await.unwrap();
        let addr = server.local_addr().unwrap();
        tokio::spawn(async move {
            let mut buf = [0u8; 512];
            let (_, peer) = server.recv_from(&mut buf).await.unwrap();
            let reply = response_with(true, IDENTIFIER, AUTH_TAG);
            server.send_to(&reply, peer).await.unwrap();
        });

        let located = locate_via(
            &store,
            UDID,
            Some(Hint::with_port(addr.ip(), addr.port())),
            Duration::from_secs(1),
        )
        .await
        .unwrap();
        assert_eq!(located.record.udid, UDID);
        assert_eq!(located.service_identifier, IDENTIFIER);
        assert_eq!(located.address.ip(), "127.0.0.1".parse::<IpAddr>().unwrap());
        assert_eq!(located.address.port(), 49152);
        tokio::fs::remove_dir_all(store.directory()).await.ok();
    }

    /// An answer whose tag none of our keys produce is never taken for our device; with our
    /// device's name it is reported as "forgot this host" if the real one never shows up.
    #[tokio::test]
    async fn a_foreign_tag_is_never_taken_for_our_device() {
        let store = store_with_pairing().await;
        let server = UdpSocket::bind("127.0.0.1:0").await.unwrap();
        let addr = server.local_addr().unwrap();
        tokio::spawn(async move {
            let mut buf = [0u8; 512];
            let (_, peer) = server.recv_from(&mut buf).await.unwrap();
            let reply = response_with(true, IDENTIFIER, "AAAAAAAA");
            server.send_to(&reply, peer).await.unwrap();
        });
        let records = store.list().await.unwrap();
        let wanted = Wanted {
            identifier: UDID.to_string(),
            udid: Some(records[0].udid.clone()),
            name: Some(records[0].name.clone()),
            stored_identifier: records[0].service_identifier.clone(),
        };
        let devices = query(
            Hint::with_port(addr.ip(), addr.port()),
            ServiceKind::RemotePairing,
            Duration::from_secs(1),
        )
        .await
        .unwrap();
        let mut others = Vec::new();
        let judged = judge(&store, &wanted, &devices[0], &mut others)
            .await
            .unwrap();
        assert!(matches!(judged, Judged::Skip));
        assert_eq!(
            judge_foreign(wanted.name.as_deref(), &others),
            Verdict::Lost("Salon".into())
        );
        tokio::fs::remove_dir_all(store.directory()).await.ok();
    }
}
