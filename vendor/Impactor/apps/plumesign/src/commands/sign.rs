use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::Result;
use clap::Args;

use plume_core::{CertificateIdentity, CertificatePolicy, MobileProvision, RevokeMode};
use plume_utils::{Bundle, Package, Signer, SignerMode, SignerOptions};

use crate::{
    commands::{
        account::{get_authenticated_account, resolve_team},
        device::select_device,
    },
    get_data_path,
};

/// Prefixe des noms de machine des certificats crees par ce programme : `tvsign-<id d'installation>`.
const MACHINE_PREFIX: &str = "tvsign";
/// Un dossier `plume_stage_*` plus vieux que cela est un reste d'un plumesign tue : on le supprime.
const STALE_STAGE_AGE: Duration = Duration::from_secs(3600);

#[derive(Debug, Args)]
#[command(arg_required_else_help = true)]
pub struct SignArgs {
    /// Path to the app bundle or package to sign (.app or .ipa)
    #[arg(long, short, value_name = "PACKAGE")]
    pub package: PathBuf,
    /// PEM files for certificate and private key
    #[arg(long = "pem", value_name = "PEM", num_args = 1..)]
    pub pem_files: Option<Vec<PathBuf>>,
    /// Use Apple ID credentials for signing
    #[arg(long = "apple-id")]
    pub apple_id: bool,
    /// Developer team to sign for (default: the team kept for the account, see `account set-team`)
    #[arg(long = "team", value_name = "TEAM_ID")]
    pub team_id: Option<String>,
    /// Which certificates may be revoked when the account is full: never, own (default: only the
    /// ones this install created, plus dead ones) or any (Xcode, SideStore... too).
    /// Falls back to $PLUMESIGN_REVOKE.
    #[arg(long = "revoke", value_name = "MODE")]
    pub revoke: Option<String>,
    /// Never revoke a live certificate (same as --revoke never, or $PLUMESIGN_NO_REVOKE=1)
    #[arg(long = "no-revoke")]
    pub no_revoke: bool,
    /// Provisioning profile files to embed
    #[arg(long = "provision", value_name = "PROVISION")]
    pub provisioning_files: Option<PathBuf>,
    /// Custom bundle identifier to set
    #[arg(long = "custom-identifier", value_name = "BUNDLE_ID")]
    pub bundle_identifier: Option<String>,
    /// Custom bundle name to set
    #[arg(long = "custom-name", value_name = "NAME")]
    pub name: Option<String>,
    /// Custom bundle version to set
    #[arg(long = "custom-version", value_name = "VERSION")]
    pub version: Option<String>,
    /// Perform ad-hoc signing (no certificate required)
    #[arg(long, short, num_args = 1..)]
    pub tweaks: Option<Vec<PathBuf>>,
    /// Register device and install after signing
    #[arg(long)]
    pub register_and_install: bool,
    /// Device UDID to register and install to (will prompt if not provided)
    #[arg(long, value_name = "UDID")]
    pub udid: Option<String>,
    /// Output path for signed .ipa (only for .ipa input)
    #[arg(long, short, value_name = "OUTPUT")]
    pub output: Option<PathBuf>,
    /// Install to connected Mac (arm64 only)
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    #[arg(short = 'm', long = "mac", value_name = "MAC", conflicts_with = "udid")]
    pub mac: bool,
}

pub async fn execute(args: SignArgs) -> Result<()> {
    if !args.package.is_dir() && !args.apple_id && args.output.is_none() {
        return Err(anyhow::anyhow!(
            "-o/--output is required when signing an .ipa without --apple-id (ad-hoc mode)."
        ));
    }

    let mut options = SignerOptions {
        custom_identifier: args.bundle_identifier,
        custom_name: args.name,
        custom_version: args.version,
        tweaks: args.tweaks,
        // a profile given with --provision belongs to the user: its wildcard has to be resolved
        resolve_profile_wildcards: args.provisioning_files.is_some(),
        ..Default::default()
    };

    // Restes d'un plumesign tue avant son nettoyage (kill sur delai, coupure) : balayes au passage.
    let swept = plume_utils::sweep_stale_stages(STALE_STAGE_AGE);
    if swept > 0 {
        log::info!("Removed {swept} stale staging folder(s)");
    }

    let revoke_mode = revoke_mode(
        args.revoke.as_deref(),
        args.no_revoke,
        std::env::var("PLUMESIGN_REVOKE").ok().as_deref(),
        std::env::var("PLUMESIGN_NO_REVOKE").ok().as_deref(),
    )?;

    let (bundle, package) = if args.package.is_dir() {
        log::warn!("⚠️  Signing bundle in place: {}", args.package.display());
        if args.output.is_some() {
            log::warn!(
                "Note: -o/--output flag is ignored for .app bundles (in-place signing only)"
            );
        }
        (Bundle::new(&args.package)?, None)
    } else {
        let pkg = Package::new(args.package.clone())?;
        let guard = pkg.stage_guard();
        let bundle = pkg.get_package_bundle()?;
        (bundle, Some((pkg, guard)))
    };

    let (mut signer, team_id_opt) = if let Some(ref pem_files) = args.pem_files {
        let cert_identity = CertificateIdentity::new_with_paths(Some(pem_files.clone())).await?;

        options.mode = SignerMode::Pem;
        (Signer::new(Some(cert_identity), options), None)
    } else if args.apple_id {
        let session = get_authenticated_account().await?;
        let team_id = resolve_team(&session, args.team_id.as_deref()).await?;
        let (machine_name, family_prefix) = machine_identity(&get_data_path());
        log::info!("Certificate machine name: {machine_name} (revoke mode: {revoke_mode:?})");
        let policy = CertificatePolicy {
            revoke: revoke_mode,
            family_prefix,
        };
        let cert_identity = CertificateIdentity::new_with_session_policy(
            &session,
            get_data_path(),
            Some(machine_name),
            &team_id,
            false,
            policy,
            None,
        )
        .await?;
        if let (Some(serial), Some(expires)) =
            (cert_identity.serial_number.as_deref(), cert_identity.expires_on())
        {
            println!("CERT {serial} {expires}");
        }

        options.mode = SignerMode::Pem;
        (
            Signer::new(Some(cert_identity), options),
            Some((session, team_id)),
        )
    } else {
        options.mode = SignerMode::Adhoc;
        (Signer::new(None, options), None)
    };

    if let Some(provision_path) = args.provisioning_files {
        let prov = MobileProvision::load_with_path(&provision_path)?;
        signer.provisioning_files.push(prov.clone());
    }

    let device = if args.register_and_install {
        #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
        {
            if args.mac {
                use plume_utils::Device;

                Some(Device {
                    name: "My Mac".to_string(),
                    udid: String::new(),
                    product_type: None,
                    device_id: 0,
                    usbmuxd_device: None,
                    is_mac: true,
                })
            } else {
                Some(select_device(args.udid).await?)
            }
        }
        #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
        {
            Some(select_device(args.udid).await?)
        }
    } else {
        None
    };

    // Without a USB device, fall back to the bundle's own platform so tvOS
    // apps get a tvOS provisioning profile.
    let platform = device
        .as_ref()
        .map(|dev| dev.developer_platform())
        .unwrap_or_else(|| {
            let is_tvos = plist::from_file::<_, plist::Dictionary>(bundle.bundle_dir().join("Info.plist"))
                .ok()
                .and_then(|info| info.get("DTPlatformName").and_then(|v| v.as_string()).map(|s| s == "appletvos"))
                .unwrap_or(false);
            if is_tvos {
                log::info!("tvOS bundle detected, using tvOS developer platform");
                plume_core::developer::DeveloperPlatform::TvOs
            } else {
                Default::default()
            }
        });

    if let Some((session, team_id)) = team_id_opt {
        signer
            .modify_bundle(&bundle, &Some(team_id.clone()))
            .await?;

        if let Some(ref dev) = device {
            log::info!("Registering device: {} ({})", dev.name, dev.udid);
            session
                .qh_ensure_device(&team_id, &dev.name, &dev.udid, platform)
                .await?;
        }

        signer
            .register_bundle(&bundle, &session, &team_id, false, platform)
            .await?;
        signer.sign_bundle(&bundle).await?;

        if let Some(dev) = device {
            log::info!("Installing to device: {}", dev.name);
            #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
            if args.mac {
                plume_utils::install_app_mac(&bundle.bundle_dir()).await?;
            } else {
                dev.install_app(bundle.bundle_dir(), |progress| async move {
                    log::info!("Installation progress: {}%", progress);
                })
                .await?;
            }

            #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
            {
                dev.install_app(bundle.bundle_dir(), |progress| async move {
                    log::info!("Installation progress: {}%", progress);
                })
                .await?;
            }

            log::info!("Installation complete!");
        }
    } else {
        signer.modify_bundle(&bundle, &None).await?;
        signer.sign_bundle(&bundle).await?;

        if let Some(dev) = device {
            log::info!("Installing to device: {}", dev.name);
            dev.install_app(bundle.bundle_dir(), |progress| async move {
                log::info!("Installation progress: {}%", progress);
            })
            .await?;

            log::info!("Installation complete!");
        }
    }

    if let Some((pkg, _guard)) = package {
        if let Some(output_path) = args.output {
            let archived_path = pkg.get_archive_based_on_path(&args.package.clone())?;
            tokio::fs::copy(&archived_path, &output_path).await?;
            log::info!("Saved signed package to: {}", output_path.display());
            if std::env::var("PLUME_DELETE_AFTER_FINISHED").is_err() {
                pkg.remove_package_stage();
            }
        } else {
            log::info!("Signed .ipa successfully (not archived, use -o to save)");
            if std::env::var("PLUME_DELETE_AFTER_FINISHED").is_err() {
                pkg.remove_package_stage();
            }
        }
    }

    Ok(())
}

/// Nom de machine des certificats de cette installation (`tvsign-1a2b3c4d`, l'id est memorise a cote
/// des donnees) et prefixe commun a toutes les installations. `PLUMESIGN_MACHINE_NAME` impose un nom.
fn machine_identity(data_dir: &Path) -> (String, Option<String>) {
    if let Some(name) = std::env::var("PLUMESIGN_MACHINE_NAME")
        .ok()
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
    {
        return (name, None);
    }
    (
        format!("{MACHINE_PREFIX}-{}", install_id(data_dir)),
        Some(MACHINE_PREFIX.to_string()),
    )
}

/// 8 caracteres hexadecimaux minuscules, ou `None`.
fn parse_install_id(raw: &str) -> Option<String> {
    let id = raw.trim().to_ascii_lowercase();
    (id.len() == 8 && id.chars().all(|c| c.is_ascii_hexdigit())).then_some(id)
}

/// Identifiant propre a cette installation, cree au premier usage dans `<data>/install_id`.
fn install_id(data_dir: &Path) -> String {
    let path = data_dir.join("install_id");
    if let Some(id) = std::fs::read_to_string(&path)
        .ok()
        .and_then(|raw| parse_install_id(&raw))
    {
        return id;
    }
    let id = uuid::Uuid::new_v4().simple().to_string()[..8].to_string();
    if let Err(e) = std::fs::write(&path, format!("{id}\n")) {
        log::warn!("could not save the install id: {e}");
    }
    id
}

/// Mode de revocation : `--no-revoke` / `PLUMESIGN_NO_REVOKE=1` (never) > `--revoke` > `PLUMESIGN_REVOKE`
/// > own. Une valeur invalide est une erreur : on ne devine pas sur une operation destructive.
fn revoke_mode(
    arg: Option<&str>,
    no_revoke: bool,
    env: Option<&str>,
    env_no_revoke: Option<&str>,
) -> Result<RevokeMode> {
    let flag_set = |v: Option<&str>| {
        v.map(|v| matches!(v.trim().to_ascii_lowercase().as_str(), "1" | "true" | "yes"))
            .unwrap_or(false)
    };
    if no_revoke || flag_set(env_no_revoke) {
        return Ok(RevokeMode::Never);
    }
    match arg.or(env).map(str::trim).filter(|v| !v.is_empty()) {
        None => Ok(RevokeMode::OwnOnly),
        Some(value) => RevokeMode::parse(value).ok_or_else(|| {
            anyhow::anyhow!("invalid revoke mode {value:?}: expected never, own or any")
        }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn revoke_defaults_to_own_only() {
        assert_eq!(revoke_mode(None, false, None, None).unwrap(), RevokeMode::OwnOnly);
        assert_eq!(revoke_mode(None, false, Some(""), None).unwrap(), RevokeMode::OwnOnly);
    }

    #[test]
    fn revoke_flags_and_env() {
        assert_eq!(revoke_mode(Some("any"), false, None, None).unwrap(), RevokeMode::Any);
        assert_eq!(revoke_mode(None, false, Some("any"), None).unwrap(), RevokeMode::Any);
        // L'argument l'emporte sur l'environnement.
        assert_eq!(
            revoke_mode(Some("never"), false, Some("any"), None).unwrap(),
            RevokeMode::Never
        );
        // --no-revoke l'emporte sur tout.
        assert_eq!(revoke_mode(Some("any"), true, None, None).unwrap(), RevokeMode::Never);
        assert_eq!(revoke_mode(None, false, Some("any"), Some("1")).unwrap(), RevokeMode::Never);
    }

    #[test]
    fn revoke_invalid_value_is_an_error() {
        assert!(revoke_mode(Some("sometimes"), false, None, None).is_err());
    }

    #[test]
    fn install_id_validation() {
        assert_eq!(parse_install_id(" 1A2b3c4D\n"), Some("1a2b3c4d".into()));
        assert_eq!(parse_install_id("1a2b3c4"), None);
        assert_eq!(parse_install_id("zzzzzzzz"), None);
        assert_eq!(parse_install_id(""), None);
    }

    #[test]
    fn install_id_is_created_once_and_kept() {
        let dir = std::env::temp_dir().join(format!("plumesign-id-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let first = install_id(&dir);
        assert_eq!(first.len(), 8);
        assert_eq!(install_id(&dir), first);
        // Fichier abime : un nouvel identifiant valide le remplace.
        std::fs::write(dir.join("install_id"), "junk").unwrap();
        let second = install_id(&dir);
        assert!(parse_install_id(&second).is_some());
        assert_eq!(install_id(&dir), second);
        std::fs::remove_dir_all(&dir).ok();
    }
}
