use plist::{Dictionary, Value};

/// Platform a developer portal request targets.
///
/// The portal's URL path is `ios` for every platform; a non-iOS target is
/// selected with request fields instead.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum DeveloperPlatform {
    #[default]
    IOs,
    TvOs,
}

impl DeveloperPlatform {
    /// Platform named on a command line: `ios` (also `iphoneos`, `ipados`: the portal files
    /// iPhone and iPad under iOS) or `tvos` (`appletvos`). visionOS is not mapped: its portal
    /// request fields have not been captured from a real Xcode session.
    pub fn from_name(name: &str) -> Option<Self> {
        match name.trim().to_ascii_lowercase().as_str() {
            "ios" | "iphoneos" | "ipados" | "iphone" | "ipad" => Some(DeveloperPlatform::IOs),
            "tvos" | "appletvos" | "appletv" => Some(DeveloperPlatform::TvOs),
            _ => None,
        }
    }

    pub fn request_fields(self) -> &'static [(&'static str, &'static str)] {
        match self {
            DeveloperPlatform::IOs => &[],
            DeveloperPlatform::TvOs => &[("DTDK_Platform", "tvos"), ("subPlatform", "tvOS")],
        }
    }

    /// Empty for iOS, so those request bodies stay byte-identical to what they
    /// were before tvOS was supported.
    pub fn apply_to(self, body: &mut Dictionary) {
        for (key, value) in self.request_fields() {
            body.insert((*key).to_string(), Value::String((*value).to_string()));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_map_to_platforms() {
        assert_eq!(DeveloperPlatform::from_name("ios"), Some(DeveloperPlatform::IOs));
        assert_eq!(DeveloperPlatform::from_name("iPadOS"), Some(DeveloperPlatform::IOs));
        assert_eq!(DeveloperPlatform::from_name(" tvOS "), Some(DeveloperPlatform::TvOs));
        assert_eq!(DeveloperPlatform::from_name("appletvos"), Some(DeveloperPlatform::TvOs));
        assert_eq!(DeveloperPlatform::from_name("visionos"), None);
        assert_eq!(DeveloperPlatform::from_name("watchos"), None);
        assert_eq!(DeveloperPlatform::from_name(""), None);
    }

    #[test]
    fn default_is_ios() {
        assert_eq!(DeveloperPlatform::default(), DeveloperPlatform::IOs);
    }

    #[test]
    fn ios_leaves_the_body_untouched() {
        let mut body = Dictionary::new();
        body.insert("teamId".to_string(), Value::String("T123".to_string()));
        let original = body.clone();

        DeveloperPlatform::IOs.apply_to(&mut body);

        assert_eq!(body, original);
    }

    #[test]
    fn tvos_adds_exactly_the_two_platform_fields() {
        let mut body = Dictionary::new();
        DeveloperPlatform::TvOs.apply_to(&mut body);

        assert_eq!(body.len(), 2);
        assert_eq!(
            body.get("DTDK_Platform").and_then(Value::as_string),
            Some("tvos")
        );
        assert_eq!(
            body.get("subPlatform").and_then(Value::as_string),
            Some("tvOS")
        );
    }
}
