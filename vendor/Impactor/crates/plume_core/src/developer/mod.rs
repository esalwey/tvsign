mod platform;
pub mod qh;
mod session;
pub mod v1;

pub use platform::DeveloperPlatform;
pub use session::{DeveloperSession, RequestType};

#[macro_export]
macro_rules! developer_endpoint {
    ($endpoint:expr) => {
        format!("https://developerservices2.apple.com/services{}", $endpoint)
    };
}

// Apple apis restrict certain characters in app names
pub fn strip_invalid_chars(s: &str) -> String {
    s.chars().filter(|c| c.is_ascii_alphabetic()).collect()
}

/// Nom d'App ID / d'app group accepte par Apple : jamais vide.
/// Lettres ASCII d'abord (comportement historique), puis lettres/chiffres/espaces,
/// puis le dernier composant de l'identifiant, puis « App ».
pub fn sanitize_app_name(name: &str, identifier: &str) -> String {
    let letters = strip_invalid_chars(name);
    if !letters.is_empty() {
        return letters;
    }
    let alnum: String = name
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || *c == ' ')
        .collect();
    let alnum = alnum.trim();
    if !alnum.is_empty() {
        return alnum.to_string();
    }
    let last = strip_invalid_chars(identifier.rsplit('.').next().unwrap_or(""));
    if !last.is_empty() {
        return last;
    }
    "App".to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn latin_names_keep_their_historic_form() {
        assert_eq!(sanitize_app_name("My App 2", "com.x.y"), "MyApp");
    }

    #[test]
    fn digits_only_names_stay_usable() {
        assert_eq!(sanitize_app_name("4K", "com.x.player"), "K");
        assert_eq!(sanitize_app_name("1080", "com.x.player"), "1080");
    }

    #[test]
    fn non_latin_names_fall_back_to_the_identifier() {
        assert_eq!(sanitize_app_name("日本語プレイヤー", "com.x.player"), "player");
        assert_eq!(sanitize_app_name("Плеер", "com.x.tv-4"), "tv");
    }

    #[test]
    fn never_empty() {
        assert_eq!(sanitize_app_name("日本", "12.34"), "App");
        assert_eq!(sanitize_app_name("", ""), "App");
    }
}
