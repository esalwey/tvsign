use super::{Bundle, PlistInfoTrait};
use crate::{Error, SignerApp, SignerOptions, cgbi};
use plist::Dictionary;
use std::path::PathBuf;
use std::{env, fs, io::Read};
use uuid::Uuid;
use zip::ZipArchive;
use zip::write::FileOptions;

#[derive(Debug, Clone)]
pub struct Package {
    package_file: PathBuf,
    stage_dir: PathBuf,
    stage_payload_dir: PathBuf,
    info_plist_dictionary: Dictionary,
    archive_entries: Vec<String>,
    pub app_icon_data: Option<Vec<u8>>,
}

impl Package {
    pub fn new(package_file: PathBuf) -> Result<Self, Error> {
        let stage_dir = env::temp_dir().join(format!(
            "plume_stage_{:08}",
            Uuid::new_v4().to_string().to_uppercase()
        ));
        let out_package_file = stage_dir.join("stage.ipa");

        fs::create_dir_all(&stage_dir).ok();

        // Un paquet illisible ne doit pas laisser sa copie dans le dossier temporaire.
        Self::open_stage(package_file, stage_dir.clone(), out_package_file).map_err(|e| {
            fs::remove_dir_all(&stage_dir).ok();
            e
        })
    }

    fn open_stage(
        package_file: PathBuf,
        stage_dir: PathBuf,
        out_package_file: PathBuf,
    ) -> Result<Self, Error> {
        fs::copy(&package_file, &out_package_file)?;

        let file = fs::File::open(&out_package_file)?;
        let mut archive = ZipArchive::new(file)?;
        let archive_entries = (0..archive.len())
            .filter_map(|i| archive.by_index(i).ok().map(|f| f.name().to_string()))
            .collect::<Vec<_>>();

        let info_plist_dictionary =
            Self::get_info_plist_from_archive(&out_package_file, &archive_entries)?;

        let app_icon_data = Self::extract_icon_from_archive(
            &out_package_file,
            &archive_entries,
            &info_plist_dictionary,
        );

        Ok(Self {
            package_file: out_package_file,
            stage_dir: stage_dir.clone(),
            stage_payload_dir: stage_dir.join("Payload"),
            info_plist_dictionary,
            archive_entries,
            app_icon_data,
        })
    }

    pub fn package_file(&self) -> &PathBuf {
        &self.package_file
    }

    fn get_info_plist_from_archive(
        archive_path: &PathBuf,
        archive_entries: &[String],
    ) -> Result<Dictionary, Error> {
        let file = fs::File::open(archive_path)?;
        let mut archive = ZipArchive::new(file)?;

        let info_plist_path = archive_entries
            .iter()
            .find(|entry| {
                entry.starts_with("Payload/")
                    && entry.ends_with("/Info.plist")
                    && entry.matches('/').count() == 2
            })
            .ok_or(Error::PackageInfoPlistMissing)?;

        let mut plist_file = archive.by_name(info_plist_path)?;
        let mut plist_data = Vec::new();
        plist_file.read_to_end(&mut plist_data)?;

        Ok(plist::from_bytes(&plist_data)?)
    }

    fn extract_icon_from_archive(
        archive_path: &PathBuf,
        archive_entries: &[String],
        plist: &Dictionary,
    ) -> Option<Vec<u8>> {
        // Collects all candidate icon base names from the plist, in order of preference.
        // CFBundleIcons (iPhone) takes priority, fall back to CFBundleIcons~ipad, then
        // top-level CFBundleIconFiles.
        let mut icon_names: Vec<String> = Vec::new();

        let primary_from = |d: &Dictionary| -> Vec<String> {
            d.get("CFBundlePrimaryIcon")
                .and_then(|v| v.as_dictionary())
                .and_then(|d| d.get("CFBundleIconFiles"))
                .and_then(|v| v.as_array())
                .map(|arr| {
                    arr.iter()
                        .filter_map(|v| v.as_string())
                        .map(|s| s.to_string())
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default()
        };

        if let Some(d) = plist.get("CFBundleIcons").and_then(|v| v.as_dictionary()) {
            icon_names.extend(primary_from(d));
        }
        if let Some(d) = plist
            .get("CFBundleIcons~ipad")
            .and_then(|v| v.as_dictionary())
        {
            for n in primary_from(d) {
                if !icon_names.contains(&n) {
                    icon_names.push(n);
                }
            }
        }
        if let Some(arr) = plist.get("CFBundleIconFiles").and_then(|v| v.as_array()) {
            for n in arr
                .iter()
                .filter_map(|v| v.as_string())
                .map(|s| s.to_string())
            {
                if !icon_names.contains(&n) {
                    icon_names.push(n);
                }
            }
        }

        if icon_names.is_empty() {
            return None;
        }

        let app_prefix = archive_entries
            .iter()
            .find(|e| {
                e.starts_with("Payload/")
                    && e.ends_with("/Info.plist")
                    && e.matches('/').count() == 2
            })?
            .trim_end_matches("/Info.plist");

        let file = fs::File::open(archive_path).ok()?;
        let mut archive = ZipArchive::new(file).ok()?;

        let suffixes = ["@3x.png", "@2x.png", "@1x.png", ".png"];

        for name in &icon_names {
            for suffix in &suffixes {
                let candidate = format!("{app_prefix}/{name}{suffix}");
                if let Ok(mut entry) = archive.by_name(&candidate) {
                    let mut data = Vec::new();
                    if entry.read_to_end(&mut data).is_ok() && !data.is_empty() {
                        return Some(cgbi::normalize(data));
                    }
                }
            }
        }

        None
    }

    pub fn get_package_bundle(&self) -> Result<Bundle, Error> {
        let file = fs::File::open(&self.package_file)?;
        let mut archive = ZipArchive::new(file)?;
        archive.extract(&self.stage_dir)?;

        let app_dir = fs::read_dir(&self.stage_payload_dir)?
            .filter_map(Result::ok)
            .map(|e| e.path())
            .find(|p| p.is_dir() && p.extension().and_then(|e| e.to_str()) == Some("app"))
            .ok_or_else(|| Error::PackageInfoPlistMissing)?;

        Ok(Bundle::new(app_dir)?)
    }

    pub fn get_archive_based_on_path(&self, path: &PathBuf) -> Result<PathBuf, Error> {
        // A directory was signed in place; an archive must be rebuilt from the
        // signed stage, otherwise the original (unsigned) file would be returned.
        if path.is_dir() {
            Ok(self.package_file.clone())
        } else {
            self.clone().archive_package_bundle()
        }
    }

    fn archive_package_bundle(self) -> Result<PathBuf, Error> {
        let zip_file_path = self.stage_dir.join("resigned.ipa");
        let file = fs::File::create(&zip_file_path)?;
        let mut zip = zip::ZipWriter::new(file);
        let options = FileOptions::default().compression_method(zip::CompressionMethod::Deflated);

        let payload_dir = self.stage_payload_dir;

        fn add_dir_to_zip(
            zip: &mut zip::ZipWriter<fs::File>,
            path: &PathBuf,
            prefix: &PathBuf,
            options: &FileOptions<'_, zip::write::ExtendedFileOptions>,
        ) -> Result<(), Error> {
            for entry in fs::read_dir(path)? {
                let entry = entry?;
                let entry_path = entry.path();
                let name = entry_path
                    .strip_prefix(prefix)
                    .map_err(|_| Error::PackageInfoPlistMissing)?
                    .to_string_lossy()
                    .replace('\\', "/");

                if entry_path.is_file() {
                    zip.start_file(&name, options.clone())?;
                    let mut f = fs::File::open(&entry_path)?;
                    std::io::copy(&mut f, zip)?;
                } else if entry_path.is_dir() {
                    zip.add_directory(&name, options.clone())?;
                    add_dir_to_zip(zip, &entry_path, prefix, options)?;
                }
            }
            Ok(())
        }

        add_dir_to_zip(&mut zip, &payload_dir, &self.stage_dir, &options)?;
        zip.finish()?;

        Ok(zip_file_path)
    }

    pub fn remove_package_stage(self) {
        fs::remove_dir_all(&self.stage_dir).ok();
    }

    /// Dossier de travail du paquet (copie de l'IPA et contenu extrait).
    pub fn stage_dir(&self) -> &PathBuf {
        &self.stage_dir
    }

    /// Garde qui supprime le dossier de travail quand elle est lachee, sur tous les chemins de sortie
    /// (erreur comprise). `Package` reste `Clone` et sans `Drop` : l'interface graphique en garde des copies.
    pub fn stage_guard(&self) -> StageGuard {
        StageGuard {
            dir: self.stage_dir.clone(),
        }
    }
}

/// Supprime un dossier `plume_stage_*` a la fin de la portee.
#[derive(Debug)]
pub struct StageGuard {
    dir: PathBuf,
}

impl Drop for StageGuard {
    fn drop(&mut self) {
        // Meme convention que le chemin de succes : PLUME_DELETE_AFTER_FINISHED garde le dossier (debogage).
        if env::var("PLUME_DELETE_AFTER_FINISHED").is_err() {
            fs::remove_dir_all(&self.dir).ok();
        }
    }
}

/// Supprime les dossiers `plume_stage_*` du dossier temporaire plus vieux que `max_age`
/// (processus tue avant son nettoyage). Renvoie le nombre de dossiers supprimes.
pub fn sweep_stale_stages(max_age: std::time::Duration) -> usize {
    sweep_stale_stages_in(&env::temp_dir(), max_age)
}

fn sweep_stale_stages_in(dir: &std::path::Path, max_age: std::time::Duration) -> usize {
    let Ok(entries) = fs::read_dir(dir) else {
        return 0;
    };
    let mut removed = 0;
    for entry in entries.filter_map(Result::ok) {
        let name = entry.file_name();
        if !name.to_string_lossy().starts_with("plume_stage_") {
            continue;
        }
        let path = entry.path();
        if !path.is_dir() {
            continue;
        }
        let old_enough = entry
            .metadata()
            .and_then(|m| m.modified())
            .ok()
            .and_then(|t| t.elapsed().ok())
            .is_some_and(|age| age >= max_age);
        if old_enough && fs::remove_dir_all(&path).is_ok() {
            removed += 1;
        }
    }
    removed
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    fn temp_root() -> PathBuf {
        let root = env::temp_dir().join(format!("plume-pkg-test-{}", Uuid::new_v4()));
        fs::create_dir_all(&root).unwrap();
        root
    }

    #[test]
    fn guard_removes_the_stage_on_drop() {
        let root = temp_root();
        let stage = root.join("plume_stage_X");
        fs::create_dir_all(stage.join("Payload")).unwrap();
        {
            let _guard = StageGuard { dir: stage.clone() };
            assert!(stage.exists());
        }
        assert!(!stage.exists());
        fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn sweep_removes_only_old_stage_directories() {
        let root = temp_root();
        let stage = root.join("plume_stage_OLD");
        let other = root.join("something_else");
        fs::create_dir_all(&stage).unwrap();
        fs::create_dir_all(&other).unwrap();

        // Trop recent : on n'y touche pas (un autre plumesign peut encore s'en servir).
        assert_eq!(sweep_stale_stages_in(&root, Duration::from_secs(3600)), 0);
        assert!(stage.exists());

        // Age nul : tout dossier plume_stage_* est supprime, le reste est intact.
        assert_eq!(sweep_stale_stages_in(&root, Duration::ZERO), 1);
        assert!(!stage.exists());
        assert!(other.exists());
        fs::remove_dir_all(&root).ok();
    }
}

// TODO: make bundle and package share a common trait for plist info access
macro_rules! get_plist_dict_value {
    ($self:ident, $key:expr) => {{
        $self
            .info_plist_dictionary
            .get($key)
            .and_then(|v| v.as_string())
            .map(|s| s.to_string())
    }};
}

impl PlistInfoTrait for Package {
    fn get_name(&self) -> Option<String> {
        get_plist_dict_value!(self, "CFBundleDisplayName")
            .or_else(|| get_plist_dict_value!(self, "CFBundleName"))
            .or_else(|| self.get_executable())
    }

    fn get_executable(&self) -> Option<String> {
        get_plist_dict_value!(self, "CFBundleExecutable")
    }

    fn get_bundle_identifier(&self) -> Option<String> {
        get_plist_dict_value!(self, "CFBundleIdentifier")
    }

    fn get_bundle_name(&self) -> Option<String> {
        get_plist_dict_value!(self, "CFBundleName")
    }

    fn get_version(&self) -> Option<String> {
        get_plist_dict_value!(self, "CFBundleShortVersionString")
    }

    fn get_build_version(&self) -> Option<String> {
        get_plist_dict_value!(self, "CFBundleVersion")
    }
}

impl Package {
    pub fn load_into_signer_options<'settings, 'slf: 'settings>(
        &'slf self,
        settings: &'settings mut SignerOptions,
    ) {
        let app = if self
            .archive_entries
            .iter()
            .any(|entry| entry.contains("SideStoreApp.framework"))
        {
            SignerApp::LiveContainerAndSideStore
        } else {
            SignerApp::from_bundle_identifier(self.get_bundle_identifier().as_deref())
        };

        let new_settings = SignerOptions::new_for_app(app);
        *settings = new_settings;
    }
}
