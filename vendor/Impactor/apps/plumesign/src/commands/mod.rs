use clap::{Parser, Subcommand};

pub mod account;
pub mod device;
pub mod macho;
pub mod pair;
pub mod probe;
pub mod sign;

#[derive(Debug, Parser)]
#[command(
    name = "plumesign",
    author,
    version,
    about = "iOS code signing and inspection tool",
    disable_help_subcommand = true,
    arg_required_else_help = true
)]
pub struct Cli {
    #[command(subcommand)]
    pub command: Commands,
}

#[derive(Debug, Subcommand)]
pub enum Commands {
    /// Sign an iOS app bundle with certificate and provisioning profile
    Sign(sign::SignArgs),
    /// Inspect Mach-O binaries
    MachO(macho::MachArgs),
    /// Manage Apple Developer account authentication
    Account(account::AccountArgs),
    /// Device management commands
    Device(device::DeviceArgs),
    /// Pair with a device over the network
    Pair(pair::PairArgs),
    /// Send the SRP init request of GrandSlam for a throwaway Apple ID and print the answer (diagnostic)
    #[command(hide = true)]
    ProbeGsa(probe::ProbeArgs),
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::CommandFactory;

    /// Détecte au test (pas en production) les options en double, comme deux `-t` dans `sign`.
    /// `device` n'est pas vérifié : son option `--mac` n'existe que sous macOS alors que `--udid`
    /// la cite toujours (défaut d'origine, sans effet en version release).
    #[test]
    fn command_line_definition_is_consistent() {
        for name in ["sign", "account", "pair"] {
            Cli::command()
                .find_subcommand(name)
                .unwrap_or_else(|| panic!("no {name} command"))
                .clone()
                .debug_assert();
        }
    }

    #[test]
    fn new_flags_parse() {
        let cli = Cli::try_parse_from([
            "plumesign",
            "sign",
            "-p",
            "a.ipa",
            "--apple-id",
            "--team",
            "ABCDE12345",
            "--revoke",
            "never",
        ])
        .unwrap();
        match cli.command {
            Commands::Sign(args) => {
                assert_eq!(args.team_id.as_deref(), Some("ABCDE12345"));
                assert_eq!(args.revoke.as_deref(), Some("never"));
            }
            _ => panic!("expected sign"),
        }

        let cli = Cli::try_parse_from(["plumesign", "pair", "--ip", "fe80::1%12", "--port", "49152"]).unwrap();
        match cli.command {
            Commands::Pair(args) => assert_eq!(args.ip.unwrap().scope_id, 12),
            _ => panic!("expected pair"),
        }
        assert!(Cli::try_parse_from(["plumesign", "pair", "--ip", "fe80::1%eth0", "--port", "1"]).is_err());
        assert!(Cli::try_parse_from(["plumesign", "account", "set-team", "ABCDE12345"]).is_ok());
        assert!(Cli::try_parse_from(["plumesign", "account", "teams", "--json"]).is_ok());
    }
}
