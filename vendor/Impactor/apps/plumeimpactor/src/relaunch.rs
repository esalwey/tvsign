#[cfg(any(target_os = "linux", target_os = "windows", target_os = "macos"))]
use std::io::{Read, Write};
#[cfg(any(target_os = "linux", target_os = "windows", target_os = "macos"))]
use std::net::{Ipv4Addr, TcpListener, TcpStream};

#[cfg(any(target_os = "linux", target_os = "windows", target_os = "macos"))]
const RELAUNCH_SIGNAL: &[u8] = b"show";
#[cfg(any(target_os = "linux", target_os = "windows", target_os = "macos"))]
const RELAUNCH_PORT_FILE: &str = "relaunch.port";
#[cfg(target_os = "macos")]
const SINGLE_INSTANCE_FILE: &str = "single-instance.lock";

#[cfg(any(target_os = "linux", target_os = "windows", target_os = "macos"))]
fn relaunch_port_path() -> std::path::PathBuf {
    crate::defaults::get_data_path().join(RELAUNCH_PORT_FILE)
}

#[cfg(any(target_os = "linux", target_os = "windows"))]
pub(crate) fn single_instance_key() -> String {
    crate::APP_NAME.to_string()
}

#[cfg(target_os = "macos")]
pub(crate) fn single_instance_key() -> String {
    crate::defaults::get_data_path()
        .join(SINGLE_INSTANCE_FILE)
        .to_string_lossy()
        .into_owned()
}

#[cfg(any(target_os = "linux", target_os = "windows", target_os = "macos"))]
pub(crate) fn notify_running_instance() -> Result<(), String> {
    let port_contents =
        std::fs::read_to_string(relaunch_port_path()).map_err(|e| format!("read port: {e}"))?;
    let port: u16 = port_contents
        .trim()
        .parse()
        .map_err(|e| format!("parse port: {e}"))?;

    let mut stream =
        TcpStream::connect((Ipv4Addr::LOCALHOST, port)).map_err(|e| format!("connect: {e}"))?;
    stream
        .write_all(RELAUNCH_SIGNAL)
        .map_err(|e| format!("send signal: {e}"))
}

#[cfg(any(target_os = "linux", target_os = "windows", target_os = "macos"))]
pub(crate) fn start_listener<F>(on_relaunch: F) -> Result<(), String>
where
    F: Fn() + Send + Sync + 'static,
{
    let listener =
        TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).map_err(|e| format!("bind listener: {e}"))?;
    let port = listener
        .local_addr()
        .map_err(|e| format!("resolve listener addr: {e}"))?
        .port();

    std::fs::write(relaunch_port_path(), port.to_string())
        .map_err(|e| format!("write port: {e}"))?;

    let on_relaunch = std::sync::Arc::new(on_relaunch);
    std::thread::spawn(move || {
        for incoming in listener.incoming() {
            match incoming {
                Ok(mut stream) => {
                    let mut buffer = [0_u8; 16];
                    match stream.read(&mut buffer) {
                        Ok(bytes_read) if bytes_read > 0 => {
                            if buffer[..bytes_read].starts_with(RELAUNCH_SIGNAL) {
                                on_relaunch();
                            }
                        }
                        Ok(_) => {}
                        Err(err) => log::warn!("Failed to read relaunch signal: {err}"),
                    }
                }
                Err(err) => {
                    log::warn!("Relaunch listener stopped: {err}");
                    break;
                }
            }
        }
    });

    Ok(())
}
