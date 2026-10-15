use std::io::{IsTerminal, Write};
use std::path::PathBuf;

use anyhow::{Ok, Result};
use clap::{Args, Subcommand};
use dialoguer::Select;

use plume_core::{
    AnisetteConfiguration,
    auth::Account,
    developer::{DeveloperPlatform, DeveloperSession},
};
use plume_store::AccountStore;

use crate::get_data_path;

#[derive(Debug, Args)]
#[command(arg_required_else_help = true)]
pub struct AccountArgs {
    #[command(subcommand)]
    pub command: AccountCommands,
}

#[derive(Debug, Subcommand)]
#[command(arg_required_else_help = true)]
pub enum AccountCommands {
    /// Login to Apple Developer account
    Login(LoginArgs),
    /// Logout from Apple Developer account
    Logout,
    /// List all saved accounts
    List,
    /// Switch to a different account
    Switch(SwitchArgs),
    /// List certificates for a team
    Certificates(CertificatesArgs),
    /// List devices registered to the account
    Devices(DevicesArgs),
    /// Register a new device
    RegisterDevice(RegisterDeviceArgs),
    /// List all app IDs for a team
    AppIds(AppIdsArgs),
    /// List the developer teams of the selected account
    Teams(TeamsArgs),
    /// Choose the team used for signing (kept in the account store)
    SetTeam(SetTeamArgs),
}

#[derive(Debug, Args)]
pub struct TeamsArgs {
    /// Print one JSON object per team
    #[arg(long = "json")]
    pub json: bool,
}

#[derive(Debug, Args)]
pub struct SetTeamArgs {
    /// Team ID to use from now on (see `account teams`)
    #[arg(value_name = "TEAM_ID", required = true)]
    pub team_id: String,
}

#[derive(Debug, Args)]
#[command(arg_required_else_help = true)]
pub struct LoginArgs {
    /// Apple ID email
    #[arg(short = 'u', long = "username", value_name = "EMAIL")]
    pub username: Option<String>,
    /// Password (falls back to $PLUMESIGN_PASSWORD, then a line read from stdin)
    #[arg(short = 'p', long = "password", value_name = "PASSWORD")]
    pub password: Option<String>,
}

const PASSWORD_ENV: &str = "PLUMESIGN_PASSWORD";

#[derive(Debug, Args)]
pub struct CertificatesArgs {
    /// Team ID to list certificates for
    #[arg(short = 't', long = "team", value_name = "TEAM_ID")]
    pub team_id: Option<String>,
    /// Filter by certificate type (development, distribution)
    #[arg(long = "type", value_name = "TYPE")]
    pub cert_type: Option<String>,
}

#[derive(Debug, Args)]
pub struct DevicesArgs {
    /// Team ID to list devices for
    #[arg(short = 't', long = "team", value_name = "TEAM_ID")]
    pub team_id: Option<String>,
    /// Filter by device platform (ios, tvos, watchos)
    #[arg(long = "platform", value_name = "PLATFORM")]
    pub platform: Option<String>,
}

#[derive(Debug, Args)]
pub struct RegisterDeviceArgs {
    /// Team ID to list devices for
    #[arg(short = 't', long = "team", value_name = "TEAM_ID")]
    pub team_id: Option<String>,
    /// Device UDID
    #[arg(short = 'u', long = "udid", value_name = "UDID", required = true)]
    pub udid: String,
    /// Device name
    #[arg(short = 'n', long = "name", value_name = "NAME", required = true)]
    pub name: String,
}

#[derive(Debug, Args)]
pub struct AppIdsArgs {
    /// Team ID to list app IDs for
    #[arg(short = 't', long = "team", value_name = "TEAM_ID")]
    pub team_id: Option<String>,
}

#[derive(Debug, Args)]
pub struct SwitchArgs {
    /// Email of the account to switch to
    #[arg(value_name = "EMAIL", required = true)]
    pub email: String,
}

pub async fn execute(args: AccountArgs) -> Result<()> {
    match args.command {
        AccountCommands::Login(login_args) => login(login_args).await,
        AccountCommands::Logout => logout().await,
        AccountCommands::List => list_accounts().await,
        AccountCommands::Switch(switch_args) => switch_account(switch_args).await,
        AccountCommands::Certificates(cert_args) => certificates(cert_args).await,
        AccountCommands::Devices(device_args) => devices(device_args).await,
        AccountCommands::RegisterDevice(register_args) => register_device(register_args).await,
        AccountCommands::AppIds(app_id_args) => app_ids(app_id_args).await,
        AccountCommands::Teams(teams_args) => list_teams(teams_args).await,
        AccountCommands::SetTeam(set_args) => set_team(set_args).await,
    }
}

fn get_settings_path() -> PathBuf {
    get_data_path().join("accounts.json")
}

pub async fn get_authenticated_account() -> Result<DeveloperSession> {
    let settings_path = get_settings_path();
    let settings = AccountStore::load(&Some(settings_path.clone())).await?;

    let gsa_account = settings
        .selected_account()
        .ok_or_else(|| {
            anyhow::anyhow!(
                "No account selected. Please login first using 'plumesign account login'"
            )
        })?
        .clone();

    let anisette_config = AnisetteConfiguration::default().set_configuration_path(get_data_path());

    log::info!("Restoring session for {}...", gsa_account.email());

    let session = DeveloperSession::new(
        gsa_account.adsid().clone(),
        gsa_account.xcode_gs_token().clone(),
        anisette_config,
    )
    .await?;

    Ok(session)
}

async fn login(args: LoginArgs) -> Result<()> {
    let tfa_closure = |req: plume_core::auth::TwoFactorRequest| -> std::result::Result<
        plume_core::auth::TwoFactorAction,
        String,
    > {
        use plume_core::auth::{TwoFactorAction, TwoFactorMethod};

        let can_use_sms = !req.trusted_phone_numbers.is_empty();
        match req.method {
            TwoFactorMethod::Sms => log::info!("Enter the code sent via SMS: "),
            TwoFactorMethod::Device if can_use_sms && req.trusted_phone_numbers.len() > 1 => {
                let endings: Vec<String> = req
                    .trusted_phone_numbers
                    .iter()
                    .map(|p| p.last_two_digits.to_string())
                    .collect();
                log::info!(
                    "Enter the code sent to your device, or type 'sms' to receive it by text message (numbers ending in {}; 'sms {}' picks one): ",
                    endings.join(", "),
                    endings[0]
                )
            }
            TwoFactorMethod::Device if can_use_sms => {
                log::info!("Enter the code sent to your device, or type 'sms' to receive it by text message: ")
            }
            TwoFactorMethod::Device => log::info!("Enter 2FA code: "),
        }

        let mut input = String::new();
        std::io::stdin()
            .read_line(&mut input)
            .map_err(|e| e.to_string())?;
        let input = input.trim();

        let lowered = input.to_ascii_lowercase();
        let sms_request = lowered == "sms" || lowered.starts_with("sms ") || lowered.starts_with("sms:");
        if sms_request && can_use_sms {
            use std::io::IsTerminal;
            // "sms 34" / "sms:34": the number ending in 34, without a menu, so a
            // parent process driving stdin can choose.
            let ending = lowered[3..].trim_start_matches([':', ' ']).trim().to_string();
            let picked = (!ending.is_empty())
                .then(|| {
                    req.trusted_phone_numbers
                        .iter()
                        .find(|p| p.last_two_digits.to_string().ends_with(&ending))
                })
                .flatten();
            let id = if let Some(phone) = picked {
                phone.id
            } else if req.trusted_phone_numbers.len() == 1 || !std::io::stdin().is_terminal() {
                req.trusted_phone_numbers[0].id
            } else {
                let labels: Vec<String> = req
                    .trusted_phone_numbers
                    .iter()
                    .map(|p| format!("Phone ending in {}", p.last_two_digits))
                    .collect();
                let choice = Select::new()
                    .with_prompt("Send SMS to which number?")
                    .items(&labels)
                    .default(0)
                    .interact()
                    .map_err(|e| e.to_string())?;
                req.trusted_phone_numbers[choice].id
            };
            return std::result::Result::Ok(TwoFactorAction::SendSms(id));
        }

        std::result::Result::Ok(TwoFactorAction::SubmitCode(input.to_string()))
    };

    let anisette_config = AnisetteConfiguration::default().set_configuration_path(get_data_path());

    let username = if let Some(user) = args.username {
        user
    } else {
        log::info!("Enter Apple ID email: ");
        let mut input = String::new();
        std::io::stdin().read_line(&mut input)?;
        input.trim().to_string()
    };

    // Without -p, take the password from PLUMESIGN_PASSWORD or the first line of
    // stdin, so callers can keep it out of the process arguments.
    let password = match args.password {
        Some(pass) => pass,
        None => match std::env::var(PASSWORD_ENV).ok().filter(|p| !p.is_empty()) {
            Some(pass) => pass,
            None => {
                eprint!("Enter password: ");
                std::io::stderr().flush()?;

                let mut input = String::new();
                std::io::stdin().read_line(&mut input)?;
                input.trim_end_matches(['\r', '\n']).to_string()
            }
        },
    };

    let login_closure = || -> std::result::Result<(String, String), String> {
        std::result::Result::Ok((username.clone(), password.clone()))
    };

    println!("Logging in...");
    let account = Account::login(login_closure, tfa_closure, anisette_config).await?;

    let settings_path = get_settings_path();
    let mut settings = AccountStore::load(&Some(settings_path.clone())).await?;
    settings
        .accounts_add_from_session(username, account)
        .await?;

    log::info!("Successfully logged in and account saved.");

    Ok(())
}

async fn logout() -> Result<()> {
    let settings_path = get_settings_path();
    let mut settings = AccountStore::load(&Some(settings_path.clone())).await?;

    let email = settings
        .selected_account()
        .ok_or_else(|| anyhow::anyhow!("No account currently logged in"))?
        .email()
        .clone();

    settings.accounts_remove(&email).await?;

    log::info!("Successfully logged out and removed account.");

    Ok(())
}

async fn certificates(args: CertificatesArgs) -> Result<()> {
    let session = get_authenticated_account().await?;

    let team_id = resolve_team(&session, args.team_id.as_deref()).await?;

    let p = session.qh_list_certs(&team_id).await?.certificates;

    log::info!("{:#?}", p);

    Ok(())
}

async fn devices(args: DevicesArgs) -> Result<()> {
    let session = get_authenticated_account().await?;

    let team_id = resolve_team(&session, args.team_id.as_deref()).await?;

    let p = session
        .qh_list_devices(&team_id, DeveloperPlatform::default())
        .await?
        .devices;

    log::info!("{:#?}", p);

    Ok(())
}

async fn register_device(args: RegisterDeviceArgs) -> Result<()> {
    let session = get_authenticated_account().await?;

    let team_id = resolve_team(&session, args.team_id.as_deref()).await?;

    let p = session
        .qh_add_device(
            &team_id,
            &args.name,
            &args.udid,
            DeveloperPlatform::default(),
        )
        .await?
        .device;

    log::info!("{:#?}", p);

    Ok(())
}

/// D'ou vient l'equipe retenue.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum TeamSource {
    /// `--team` sur la ligne de commande.
    Explicit,
    /// `PLUMESIGN_TEAM_ID`.
    Env,
    /// Equipe memorisee dans accounts.json (`account set-team`, ou premiere resolution).
    Stored,
    /// La seule equipe du compte.
    Only,
    /// Plusieurs equipes, rien de choisi, pas de terminal : la premiere (comportement historique).
    FirstOfSeveral,
}

/// Choix de l'equipe, sans reseau : explicite > `PLUMESIGN_TEAM_ID` > equipe memorisee > seule equipe.
/// Une equipe demandee ou memorisee qui n'existe plus est une erreur, jamais un changement silencieux
/// (l'identifiant de l'app contient l'equipe : une autre equipe installerait une seconde copie),
/// sauf quand le compte n'en a plus qu'une : la memorisee a disparu, la seule restante est reprise.
/// `Ok(None)` : plusieurs equipes sans choix, a trancher (terminal) ou a defaut la premiere.
pub(crate) fn choose_team(
    teams: &[(String, String)],
    explicit: Option<&str>,
    env: Option<&str>,
    stored: Option<&str>,
) -> std::result::Result<Option<(String, TeamSource)>, String> {
    use std::result::Result::Ok;
    if teams.is_empty() {
        return Err(
            "NO_TEAM: this Apple ID has no developer team (sign in at developer.apple.com and accept the agreement, then retry)"
                .to_string(),
        );
    }
    let listing = || {
        teams
            .iter()
            .map(|(id, name)| format!("{name} ({id})"))
            .collect::<Vec<_>>()
            .join(", ")
    };
    let exists = |id: &str| teams.iter().any(|(t, _)| t == id);
    fn clean(v: Option<&str>) -> Option<&str> {
        v.map(str::trim).filter(|v| !v.is_empty())
    }

    if let Some(wanted) = clean(explicit) {
        return if exists(wanted) {
            Ok(Some((wanted.to_string(), TeamSource::Explicit)))
        } else {
            Err(format!(
                "TEAM_NOT_FOUND: team {wanted} (--team) is not one of this account's teams: {}",
                listing()
            ))
        };
    }
    if let Some(wanted) = clean(env) {
        return if exists(wanted) {
            Ok(Some((wanted.to_string(), TeamSource::Env)))
        } else {
            Err(format!(
                "TEAM_NOT_FOUND: team {wanted} (PLUMESIGN_TEAM_ID) is not one of this account's teams: {}",
                listing()
            ))
        };
    }
    if let Some(wanted) = clean(stored) {
        return if exists(wanted) {
            Ok(Some((wanted.to_string(), TeamSource::Stored)))
        } else if teams.len() == 1 {
            // Equipe memorisee disparue (compte retire de l'equipe invitee choisie a la connexion) mais
            // il n'en reste qu'une : il n'y a pas d'ambiguite, on la reprend (resolve_team la memorise).
            Ok(Some((teams[0].0.clone(), TeamSource::Only)))
        } else {
            Err(format!(
                "TEAM_NOT_FOUND: the team {wanted} chosen for this account is no longer listed by Apple: {}. \
                 Pick another one with `account set-team`",
                listing()
            ))
        };
    }
    if teams.len() == 1 {
        return Ok(Some((teams[0].0.clone(), TeamSource::Only)));
    }
    Ok(None)
}

/// Equipe du compte selectionne, memorisee dans accounts.json.
async fn stored_team() -> (Option<String>, Option<String>) {
    match AccountStore::load(&Some(get_settings_path())).await {
        std::result::Result::Ok(store) => match store.selected_account() {
            Some(account) => {
                let team = account.team_id().trim().to_string();
                (
                    Some(account.email().clone()),
                    (!team.is_empty()).then_some(team),
                )
            }
            None => (None, None),
        },
        Err(_) => (None, None),
    }
}

/// Memorise l'equipe pour le compte (premiere resolution) : les renouvellements suivants restent
/// sur la meme equipe meme si Apple change l'ordre de sa liste.
async fn pin_team(email: &str, team_id: &str) {
    let result = async {
        let mut store = AccountStore::load(&Some(get_settings_path())).await?;
        store.update_account_team(email, team_id.to_string()).await
    }
    .await;
    if let Err(e) = result {
        log::warn!("could not remember team {team_id}: {e}");
    }
}

/// Resout l'equipe : voir `choose_team`. Plusieurs equipes sans choix : terminal -> menu, sinon la
/// premiere avec un avertissement `MULTIPLE_TEAMS`. Le resultat est memorise quand rien ne l'etait.
pub async fn resolve_team(session: &DeveloperSession, explicit: Option<&str>) -> Result<String> {
    let listed = session.qh_list_teams().await?.teams;
    let teams: Vec<(String, String)> = listed
        .iter()
        .map(|t| (t.team_id.clone(), t.name.clone()))
        .collect();

    let env_team = std::env::var("PLUMESIGN_TEAM_ID").ok();
    let (email, stored) = stored_team().await;

    let picked = choose_team(&teams, explicit, env_team.as_deref(), stored.as_deref())
        .map_err(|message| anyhow::anyhow!(message))?;

    let (team_id, source) = match picked {
        Some(found) => found,
        None => {
            if !std::io::stdin().is_terminal() {
                // Pas de terminal (service, tvsign) : jamais attendre un choix que personne ne peut faire.
                log::warn!(
                    "MULTIPLE_TEAMS: {} teams, using {} ({}); choose one with `account set-team` or PLUMESIGN_TEAM_ID",
                    teams.len(),
                    teams[0].1,
                    teams[0].0
                );
                (teams[0].0.clone(), TeamSource::FirstOfSeveral)
            } else {
                let team_names: Vec<String> = teams
                    .iter()
                    .map(|(id, name)| format!("{name} ({id})"))
                    .collect();
                let selection = Select::new().items(&team_names).default(0).interact()?;
                (teams[selection].0.clone(), TeamSource::Stored)
            }
        }
    };

    // Memorise quand rien ne l'etait, ou quand la memorisee a disparu et que la seule restante la remplace.
    if stored.is_none() || (source == TeamSource::Only && stored.as_deref() != Some(team_id.as_str())) {
        if let Some(email) = email {
            pin_team(&email, &team_id).await;
        }
    }
    log::info!("Team {team_id} ({source:?})");

    Ok(team_id)
}

async fn list_teams(args: TeamsArgs) -> Result<()> {
    let session = get_authenticated_account().await?;
    let teams = session.qh_list_teams().await?.teams;
    let (_, stored) = stored_team().await;
    let env_team = std::env::var("PLUMESIGN_TEAM_ID").ok();

    // Equipe qui serait retenue maintenant, pour la marquer dans la liste.
    let pairs: Vec<(String, String)> = teams
        .iter()
        .map(|t| (t.team_id.clone(), t.name.clone()))
        .collect();
    // Si le choix echoue (equipe memorisee disparue, plusieurs equipes), aucune n'est marquee : la
    // signature echouerait aussi.
    let current = match choose_team(&pairs, None, env_team.as_deref(), stored.as_deref()) {
        std::result::Result::Ok(Some((id, _))) => Some(id),
        std::result::Result::Ok(None) => pairs.first().map(|(id, _)| id.clone()),
        Err(_) => None,
    };

    for team in &teams {
        let selected = current.as_deref() == Some(team.team_id.as_str());
        if args.json {
            println!(
                "{}",
                serde_json::json!({
                    "team_id": team.team_id,
                    "name": team.name,
                    "type": team._type,
                    "status": team.status,
                    "selected": selected,
                })
            );
        } else {
            println!(
                "{} {} [{}] {}{}",
                team.team_id,
                team.name,
                team._type,
                team.status,
                if selected { " (selected)" } else { "" }
            );
        }
    }
    Ok(())
}

async fn set_team(args: SetTeamArgs) -> Result<()> {
    let session = get_authenticated_account().await?;
    let teams = session.qh_list_teams().await?.teams;
    let wanted = args.team_id.trim();
    if !teams.iter().any(|t| t.team_id == wanted) {
        let known = teams
            .iter()
            .map(|t| format!("{} ({})", t.name, t.team_id))
            .collect::<Vec<_>>()
            .join(", ");
        anyhow::bail!("TEAM_NOT_FOUND: team {wanted} is not one of this account's teams: {known}");
    }
    let (email, _) = stored_team().await;
    let email = email.ok_or_else(|| anyhow::anyhow!("No account selected"))?;
    let mut store = AccountStore::load(&Some(get_settings_path())).await?;
    store.update_account_team(&email, wanted.to_string()).await?;
    log::info!("Team {wanted} will be used for {email}");
    println!("TEAM {wanted}");
    Ok(())
}

pub async fn app_ids(args: AppIdsArgs) -> Result<()> {
    let session = get_authenticated_account().await?;

    let team_id = resolve_team(&session, args.team_id.as_deref()).await?;

    let p = session.v1_list_app_ids(&team_id, None).await?.data;

    log::info!("{:#?}", p);

    Ok(())
}

async fn list_accounts() -> Result<()> {
    let settings_path = get_settings_path();
    let settings = AccountStore::load(&Some(settings_path)).await?;

    let accounts = settings.accounts();

    if accounts.is_empty() {
        log::info!("No accounts found. Use 'account login' to add an account.");
        return Ok(());
    }

    let selected_email = settings.selected_account().map(|a| a.email().clone());

    log::info!("Saved accounts:");
    for (email, account) in accounts {
        let selected = if Some(email) == selected_email.as_ref() {
            "(selected)"
        } else {
            ""
        };

        log::info!(" [{}] {} {}", account.first_name(), email, selected);
    }

    Ok(())
}

async fn switch_account(args: SwitchArgs) -> Result<()> {
    let settings_path = get_settings_path();
    let mut settings = AccountStore::load(&Some(settings_path)).await?;

    if settings.get_account(&args.email).is_none() {
        return Err(anyhow::anyhow!(
            "Account '{}' not found. Use 'account list' to see available accounts.",
            args.email
        ));
    }

    settings.account_select(&args.email).await?;

    log::info!("Switched to account: {}", args.email);

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn two() -> Vec<(String, String)> {
        vec![
            ("AAAAAAAAAA".into(), "Personal".into()),
            ("BBBBBBBBBB".into(), "Company".into()),
        ]
    }

    #[test]
    fn single_team_is_used() {
        let one = vec![("AAAAAAAAAA".to_string(), "Personal".to_string())];
        assert_eq!(
            choose_team(&one, None, None, None).unwrap(),
            Some(("AAAAAAAAAA".into(), TeamSource::Only))
        );
    }

    #[test]
    fn stored_team_beats_the_listing_order() {
        let picked = choose_team(&two(), None, None, Some("BBBBBBBBBB")).unwrap();
        assert_eq!(picked, Some(("BBBBBBBBBB".into(), TeamSource::Stored)));
    }

    #[test]
    fn explicit_and_env_beat_stored_in_that_order() {
        let picked = choose_team(&two(), Some("AAAAAAAAAA"), Some("BBBBBBBBBB"), Some("BBBBBBBBBB"));
        assert_eq!(picked.unwrap(), Some(("AAAAAAAAAA".into(), TeamSource::Explicit)));
        let picked = choose_team(&two(), None, Some(" AAAAAAAAAA "), Some("BBBBBBBBBB"));
        assert_eq!(picked.unwrap(), Some(("AAAAAAAAAA".into(), TeamSource::Env)));
    }

    #[test]
    fn a_mistyped_or_vanished_team_is_an_error() {
        let err = choose_team(&two(), None, Some("ZZZZZZZZZZ"), None).unwrap_err();
        assert!(err.starts_with("TEAM_NOT_FOUND"));
        assert!(err.contains("Personal") && err.contains("BBBBBBBBBB"));
        let err = choose_team(&two(), Some("ZZZZZZZZZZ"), None, None).unwrap_err();
        assert!(err.starts_with("TEAM_NOT_FOUND"));
        let err = choose_team(&two(), None, None, Some("CCCCCCCCCC")).unwrap_err();
        assert!(err.starts_with("TEAM_NOT_FOUND") && err.contains("set-team"));
    }

    #[test]
    fn a_vanished_stored_team_falls_back_to_the_only_remaining_one() {
        let one = vec![("AAAAAAAAAA".to_string(), "Alpha".to_string())];
        assert_eq!(
            choose_team(&one, None, None, Some("CCCCCCCCCC")).unwrap(),
            Some(("AAAAAAAAAA".into(), TeamSource::Only))
        );
        // An explicit request or PLUMESIGN_TEAM_ID that does not exist stays an error.
        assert!(choose_team(&one, Some("CCCCCCCCCC"), None, None).is_err());
        assert!(choose_team(&one, None, Some("CCCCCCCCCC"), None).is_err());
    }

    #[test]
    fn several_teams_without_a_choice_are_left_to_the_caller() {
        assert_eq!(choose_team(&two(), None, None, None).unwrap(), None);
        // Variable vide ou espaces : comme absente.
        assert_eq!(choose_team(&two(), Some(""), Some("  "), Some("")).unwrap(), None);
    }

    #[test]
    fn no_team_is_reported_as_such() {
        let err = choose_team(&[], None, None, None).unwrap_err();
        assert!(err.starts_with("NO_TEAM"));
    }
}
