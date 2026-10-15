//! One-time pairing of an iPhone or iPad over USB.
//!
//! An iPhone shows no "pair with a PIN" screen like an Apple TV does, so the RemotePairing keys are
//! created through the USB cable instead: usbmuxd -> CoreDeviceProxy -> the untrusted tunnel
//! service, which accepts the fixed PIN `000000`. The resulting pairing file works afterwards over
//! Wi-Fi, exactly like one made with an Apple TV's PIN.
//!
//! Needs usbmuxd: Apple Mobile Device Service on Windows (iTunes or the Apple Devices app), built
//! into macOS, the `usbmuxd` package on Linux.

use idevice::core_device_proxy::CoreDeviceProxy;
use idevice::lockdown::LockdownClient;
use idevice::remote_pairing::{RemotePairingClient, RpPairingFile};
use idevice::rsd::RsdHandshake;
use idevice::usbmuxd::{Connection, UsbmuxdAddr, UsbmuxdConnection, UsbmuxdDevice};
use idevice::{IdeviceService, RemoteXpcClient};

use super::PairedDevice;
use crate::Error;

const LABEL: &str = "tvsign_pair";
const TUNNEL_SERVICE: &str = "com.apple.internal.dt.coredevice.untrusted.tunnelservice";
const PIN: &str = "000000";

/// What lockdown says about the device, for the pairing record and the caller.
#[derive(Debug, Clone, Default)]
pub struct UsbDeviceInfo {
    pub udid: String,
    pub name: Option<String>,
    pub product_type: Option<String>,
    pub product_version: Option<String>,
}

/// The pairing file, the record to store it under, and the device's identity.
pub struct UsbPairing {
    pub pairing_file: RpPairingFile,
    pub device: PairedDevice,
    pub info: UsbDeviceInfo,
}

/// Picks the device to pair: the given UDID, or the only one plugged in.
pub fn select_device(
    devices: Vec<UsbmuxdDevice>,
    udid: Option<&str>,
) -> Result<UsbmuxdDevice, Error> {
    let usb: Vec<_> = devices
        .into_iter()
        .filter(|d| matches!(d.connection_type, Connection::Usb))
        .collect();
    match udid {
        Some(wanted) => usb
            .into_iter()
            .find(|d| d.udid.eq_ignore_ascii_case(wanted))
            .ok_or_else(|| {
                Error::Other(format!(
                    "No device {wanted} connected over USB (unlock it and accept \"Trust this computer\")"
                ))
            }),
        None => match usb.len() {
            0 => Err(Error::Other(
                "No iPhone or iPad connected over USB (plug it in, unlock it and accept \"Trust this computer\")"
                    .to_string(),
            )),
            1 => Ok(usb.into_iter().next().expect("one element")),
            n => Err(Error::Other(format!(
                "{n} devices are connected over USB: pass the one to pair with --udid"
            ))),
        },
    }
}

fn lockdown_string(values: &plist::Value, key: &str) -> Option<String> {
    values
        .as_dictionary()
        .and_then(|d| d.get(key))
        .and_then(|v| v.as_string())
        .filter(|s| !s.is_empty())
        .map(str::to_string)
}

/// Pairs with the device plugged in over USB, as the host called `host`.
pub async fn pair_usb(udid: Option<&str>, host: &str) -> Result<UsbPairing, Error> {
    let mut usbmuxd = UsbmuxdConnection::default().await.map_err(|e| {
        Error::Other(format!(
            "usbmuxd is not reachable ({e}): install iTunes or Apple Devices on Windows, or the usbmuxd package on Linux"
        ))
    })?;
    let device = select_device(usbmuxd.get_devices().await?, udid)?;
    let provider = device.to_provider(UsbmuxdAddr::default(), LABEL);

    // Identity first: lockdown answers these keys without a session.
    let mut info = UsbDeviceInfo {
        udid: device.udid.clone(),
        ..Default::default()
    };
    if let Ok(mut lockdown) = LockdownClient::connect(&provider).await {
        if let Ok(values) = lockdown.get_value(None, None).await {
            info.name = lockdown_string(&values, "DeviceName");
            info.product_type = lockdown_string(&values, "ProductType");
            info.product_version = lockdown_string(&values, "ProductVersion");
        }
    }

    let proxy = CoreDeviceProxy::connect(&provider).await?;
    let rsd_port = proxy.tunnel_info().server_rsd_port;
    let adapter = proxy.create_software_tunnel()?;
    let mut adapter = adapter.to_async_handle();

    let stream = adapter.connect(rsd_port).await?;
    let handshake = RsdHandshake::new(stream).await?;
    let service_port = handshake
        .services
        .get(TUNNEL_SERVICE)
        .ok_or_else(|| {
            Error::Other(
                "The device offers no tunnel service (iOS 17 or later is required)".to_string(),
            )
        })?
        .port;

    let mut pairing_file = RpPairingFile::generate(host);

    // The first connection creates the keys, the second proves the device accepts them.
    let mut peer = {
        let stream = adapter.connect(service_port).await?;
        let mut xpc = RemoteXpcClient::new(stream).await?;
        xpc.do_handshake().await?;
        let _ = xpc.recv_root().await;
        let mut client = RemotePairingClient::new(xpc, host, &mut pairing_file);
        client.connect(async |_| PIN.to_string(), ()).await?;
        PairedDevice::from(client.paired_peer_device()?)
    };
    {
        let stream = adapter.connect(service_port).await?;
        let mut xpc = RemoteXpcClient::new(stream).await?;
        xpc.do_handshake().await?;
        let _ = xpc.recv_root().await;
        let mut client = RemotePairingClient::new(xpc, host, &mut pairing_file);
        client.connect(async |_| PIN.to_string(), ()).await?;
    }

    if peer.udid.is_empty() {
        peer.udid = device.udid.clone();
    }
    if let Some(name) = &info.name {
        peer.name = name.clone();
    }
    if let Some(model) = &info.product_type {
        peer.model = model.clone();
    }
    peer.account_id = host.to_string();

    Ok(UsbPairing {
        pairing_file,
        device: peer,
        info,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dev(udid: &str, connection_type: Connection, device_id: u32) -> UsbmuxdDevice {
        UsbmuxdDevice {
            connection_type,
            udid: udid.to_string(),
            device_id,
        }
    }

    #[test]
    fn the_only_usb_device_is_picked() {
        let found = select_device(
            vec![
                dev("A", Connection::Network("192.168.1.5".parse().unwrap()), 1),
                dev("B", Connection::Usb, 2),
            ],
            None,
        )
        .unwrap();
        assert_eq!(found.udid, "B");
    }

    #[test]
    fn several_devices_need_an_explicit_udid() {
        let all = || {
            vec![
                dev("AAA", Connection::Usb, 1),
                dev("BBB", Connection::Usb, 2),
            ]
        };
        assert!(select_device(all(), None).is_err());
        assert_eq!(select_device(all(), Some("bbb")).unwrap().udid, "BBB");
        assert!(select_device(all(), Some("CCC")).is_err());
    }

    #[test]
    fn no_device_is_an_error() {
        assert!(select_device(vec![], None).is_err());
    }
}
