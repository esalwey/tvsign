; tvsign - per-user installer (no administrator rights, no UAC prompt).
; Compiled by build.ps1:  ISCC.exe /DAppVersion=0.9.0 installer\tvsign.iss
; Input: ..\dist\tvsign\ (PyInstaller onedir). Output: ..\dist\installer\tvsign-setup-<version>.exe

#ifndef AppVersion
  #define AppVersion "0.9.0"
#endif
#ifndef AppNumVersion
  #define AppNumVersion AppVersion
#endif
#define AppName "tvsign"
#define AppExe "tvsign.exe"
#define SourceDir "..\dist\tvsign"

[Setup]
AppId={{6B0E5C1D-7A43-4C2B-9E51-2F7D4A1B8C90}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=esalwey
VersionInfoVersion={#AppNumVersion}
VersionInfoProductName={#AppName}
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\{#AppName}
DisableDirPage=yes
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
SetupIconFile=..\assets\tvsign.ico
WizardSmallImageFile=..\assets\wizard-small.bmp
WizardStyle=modern
OutputDir=..\dist\installer
OutputBaseFilename=tvsign-setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
; a running tvsign is asked to exit with "tvsign.exe --quit" (PrepareToInstall, uninstall);
; no AppMutex, so the user is not asked to close it by hand first
CloseApplications=yes
RestartApplications=no
ShowLanguageDialog=auto

[Languages]
Name: "fr"; MessagesFile: "compiler:Languages\French.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
fr.AutoStart=Démarrer tvsign avec Windows (recommandé : les apps sont renouvelées en arrière-plan)
en.AutoStart=Start tvsign with Windows (recommended: apps are renewed in the background)
fr.Options=Options :
en.Options=Options:
fr.LaunchNow=Lancer tvsign
en.LaunchNow=Launch tvsign
fr.RemoveDataTitle=Désinstaller tvsign
en.RemoveDataTitle=Uninstall tvsign
fr.RemoveDataText=tvsign va être désinstallé. Vos données (session Apple, appairages Apple TV, apps et réglages) sont conservées pour une réinstallation, sauf si vous cochez la case ci-dessous.
en.RemoveDataText=tvsign will be uninstalled. Your data (Apple session, Apple TV pairings, apps and settings) is kept for a later reinstall unless you tick the box below.
fr.RemoveDataBox=Supprimer aussi mes données tvsign
en.RemoveDataBox=Also delete my tvsign data
fr.Continue=Désinstaller
en.Continue=Uninstall
fr.Cancel=Annuler
en.Cancel=Cancel
fr.FirewallTitle=Autorisation du Pare-feu Windows
en.FirewallTitle=Windows Firewall permission
fr.FirewallSub=À lire avant le premier lancement
en.FirewallSub=Read before the first launch
fr.FirewallNote=Au premier lancement, Windows affiche une alerte du Pare-feu Windows Defender à propos de plumesign (l'outil qui parle à l'Apple TV).%n%nCochez « Réseaux privés », puis cliquez sur « Autoriser l'accès ». Sans cela, tvsign ne voit pas l'Apple TV et l'appairage échoue.%n%nUn compte non administrateur ne peut pas accepter cette alerte : demandez à un administrateur de l'autoriser. Votre réseau domestique doit aussi être de type « Privé » (Paramètres > Réseau et Internet).
en.FirewallNote=On first launch, Windows shows a Windows Defender Firewall alert about plumesign (the tool that talks to the Apple TV).%n%nTick "Private networks", then click "Allow access". Without it, tvsign can't see the Apple TV and pairing fails.%n%nA non-administrator account can't accept this alert: ask an administrator to allow it. Your home network must also be of type "Private" (Settings > Network & Internet).
fr.Firewall=Autoriser plumesign dans le Pare-feu Windows (réseaux privés), pour trouver l'Apple TV. Windows demandera une confirmation administrateur maintenant, plutôt qu'une invite surprise au premier appairage
en.Firewall=Allow plumesign through Windows Firewall (private networks) so it can find the Apple TV. Windows will ask for administrator confirmation now, instead of a surprise prompt at the first pairing
fr.QuitWait=Fermeture de tvsign : s'il est en plein renouvellement, il le termine d'abord (jusqu'à 5 minutes).
en.QuitWait=Closing tvsign: if it is in the middle of a renewal, it finishes it first (up to 5 minutes).
fr.FirewallFailed=La règle de pare-feu n'a pas été créée (confirmation refusée ?). Ce n'est pas bloquant : Windows proposera l'autorisation au premier appairage de l'Apple TV, ou voir le guide Windows de tvsign (section pare-feu).
en.FirewallFailed=The firewall rule was not created (confirmation declined?). This is not blocking: Windows will offer the permission at the first Apple TV pairing, or see the tvsign Windows guide (firewall section).
fr.WebView2Missing=Microsoft Edge WebView2 est introuvable sur ce PC. tvsign en a besoin pour afficher sa fenêtre (sans lui, l'interface s'ouvre dans le navigateur depuis l'icône près de l'horloge).%n%nOuvrir la page de téléchargement de WebView2 ?
en.WebView2Missing=Microsoft Edge WebView2 was not found on this PC. tvsign needs it to show its window (without it, the interface opens in your browser from the tray icon).%n%nOpen the WebView2 download page?

[Tasks]
Name: "autostart"; Description: "{cm:AutoStart}"; GroupDescription: "{cm:Options}"
Name: "firewall"; Description: "{cm:Firewall}"; GroupDescription: "{cm:Options}"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README-windows.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\..\LICENSE"; DestDir: "{app}\licenses"; Flags: ignoreversion
Source: "..\..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}\licenses"; Flags: ignoreversion
Source: "..\..\vendor\Impactor\LICENSE"; DestDir: "{app}\licenses"; DestName: "plumesign-LICENSE.txt"; Flags: ignoreversion

[InstallDelete]
; a previous version's runtime is replaced as a whole
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[INI]
; Start menu link to the online guide (English and French): SmartScreen, firewall, sleep...
Filename: "{autoprograms}\tvsign guide.url"; Section: "InternetShortcut"; Key: "URL"; String: "https://github.com/esalwey/tvsign/blob/main/windows/README-windows.md"

; The autostart entry (HKCU Run) is written by [Code] (CurStepChanged): a [Registry] entry would be put back by
; every upgrade even after the user switched it off from the tray, and an entry created later from the tray
; would not be known to the uninstaller.

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchNow}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: files; Name: "{autoprograms}\tvsign guide.url"
Type: filesandordirs; Name: "{app}\_internal"

[Code]
const
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';
  ApprovedKey = 'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run';
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{6B0E5C1D-7A43-4C2B-9E51-2F7D4A1B8C90}_is1';
  FirewallRule = 'tvsign plumesign';
  WakeTask = 'tvsign wake';
  WebView2Client = 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WebView2Url = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703';

var
  RemoveData: Boolean;

{ ---------- Windows Firewall rule (announced: a task of the wizard, elevated on request) ---------- }

function FirewallRuleExists(): Boolean;
var
  Code: Integer;
begin
  { "show rule" needs no elevation; exit code 0 = found, 1 = no rule with that name }
  Result := Exec(ExpandConstant('{sys}\netsh.exe'), 'advfirewall firewall show rule name="' + FirewallRule + '"',
    '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
end;

procedure AddFirewallRule();
var
  Code: Integer;
  Params: String;
begin
  { inbound, plumesign.exe only, private and domain networks: mDNS answers of the Apple TV. A rule left by an install
    in another folder would keep pointing at the old plumesign.exe: update its program path instead of skipping it
    (a single elevated command either way) }
  if FirewallRuleExists() then
    Params := 'advfirewall firewall set rule name="' + FirewallRule + '" new program="' +
      ExpandConstant('{app}\plumesign.exe') + '" profile=private,domain enable=yes'
  else
    Params := 'advfirewall firewall add rule name="' + FirewallRule + '" dir=in action=allow program="' +
      ExpandConstant('{app}\plumesign.exe') + '" profile=private,domain enable=yes';
  if ShellExec('runas', ExpandConstant('{sys}\netsh.exe'), Params, '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0) then
    Log('Firewall rule created')
  else
  begin
    Log('Firewall rule not created, code ' + IntToStr(Code));
    if not WizardSilent() then
      MsgBox(CustomMessage('FirewallFailed'), mbInformation, MB_OK);
  end;
end;

procedure RemoveFirewallRule();
var
  Code: Integer;
begin
  if FirewallRuleExists() then
    ShellExec('runas', ExpandConstant('{sys}\netsh.exe'), 'advfirewall firewall delete rule name="' + FirewallRule + '"',
      '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

{ ---------- autostart (HKCU Run) ---------- }

var
  WasInstalled, HadAutostart, TasksPageSeen: Boolean;
  FirewallPage: TOutputMsgWizardPage;

{ Registered after the files are copied: explains the Windows Firewall prompt the first discovery triggers, but is
  skipped when the rule created by the "firewall" task is in place (ShouldSkipPage below). }
procedure InitializeWizard();
begin
  WasInstalled := RegKeyExists(HKCU, UninstallKey);
  HadAutostart := RegValueExists(HKCU, RunKey, '{#AppName}');
  FirewallPage := CreateOutputMsgPage(wpInstalling, CustomMessage('FirewallTitle'), CustomMessage('FirewallSub'),
    CustomMessage('FirewallNote'));
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (PageID = FirewallPage.ID) and FirewallRuleExists();
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  { upgrade: the task starts from what the user has now (a switch made from the tray menu is respected),
    not from what was ticked at the first install; set once so that going back keeps the user's choice }
  if (CurPageID = wpSelectTasks) and WasInstalled and not TasksPageSeen then
  begin
    TasksPageSeen := True;
    if HadAutostart then
      WizardSelectTasks('autostart')
    else
      WizardSelectTasks('!autostart');
  end;
end;

procedure ApplyAutostart();
begin
  { silent upgrade without an explicit task list: leave the autostart entry exactly as the user has it }
  if WasInstalled and WizardSilent() and (ExpandConstant('{param:TASKS|}') = '') and
     (ExpandConstant('{param:MERGETASKS|}') = '') then
    Exit;
  if WizardIsTaskSelected('autostart') then
  begin
    { a "disabled" flag set by Windows (Task Manager, Settings) is respected when the entry already exists }
    if not RegValueExists(HKCU, RunKey, '{#AppName}') then
      RegWriteBinaryValue(HKCU, ApprovedKey, '{#AppName}', #2#0#0#0#0#0#0#0#0#0#0#0);
    RegWriteStringValue(HKCU, RunKey, '{#AppName}', '"' + ExpandConstant('{app}\{#AppExe}') + '" --tray');
  end
  else
    RegDeleteValue(HKCU, RunKey, '{#AppName}');
end;

{ ---------- WebView2 runtime ---------- }

function WebView2Installed(): Boolean;
var
  Version: String;
begin
  Result := (RegQueryStringValue(HKLM32, WebView2Client, 'pv', Version) or
             RegQueryStringValue(HKCU, WebView2Client, 'pv', Version)) and (Version <> '') and (Version <> '0.0.0.0');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Code: Integer;
begin
  if CurStep <> ssPostInstall then Exit;
  ApplyAutostart();
  if WizardIsTaskSelected('firewall') and not WizardSilent() then
    AddFirewallRule();
  if not WizardSilent() and not WebView2Installed() then
    if MsgBox(CustomMessage('WebView2Missing'), mbConfirmation, MB_YESNO) = IDYES then
      ShellExec('open', WebView2Url, '', '', SW_SHOWNORMAL, ewNoWait, Code);
end;

{ Ask a running tvsign to exit cleanly (stops plumesign too). Instant when idle; during a renewal --quit waits for it
  to finish, up to about 5 minutes (the wizard says so, see PrepareToInstall). Also used by the uninstaller, so no
  wizard control here. }
procedure QuitRunningApp();
var
  Code: Integer;
begin
  if FileExists(ExpandConstant('{app}\{#AppExe}')) then
    Exec(ExpandConstant('{app}\{#AppExe}'), '--quit', '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

{ Before copying files (upgrade). }
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if FileExists(ExpandConstant('{app}\{#AppExe}')) and not WizardSilent() then
  begin
    { --quit waits for a renewal in progress: say so instead of looking hung }
    WizardForm.PreparingLabel.Caption := CustomMessage('QuitWait');
    WizardForm.PreparingLabel.Update;
  end;
  QuitRunningApp();
end;

{ Uninstall: the data folder is kept unless the user ticks the box. }
function InitializeUninstall(): Boolean;
var
  Form: TSetupForm;
  Text: TNewStaticText;
  Box: TNewCheckBox;
  Ok, Cancel: TNewButton;
begin
  RemoveData := False;
  Result := True;
  if UninstallSilent then
  begin
    QuitRunningApp();   { files of a running tvsign would stay locked }
    Exit;
  end;
  Form := CreateCustomForm(ScaleX(440), ScaleY(170), False, True);
  try
    Form.Caption := CustomMessage('RemoveDataTitle');

    Text := TNewStaticText.Create(Form);
    Text.Parent := Form;
    Text.Left := ScaleX(16);
    Text.Top := ScaleY(14);
    Text.Width := Form.ClientWidth - ScaleX(32);
    Text.AutoSize := False;
    Text.WordWrap := True;
    Text.Height := ScaleY(60);
    Text.Caption := CustomMessage('RemoveDataText');

    Box := TNewCheckBox.Create(Form);
    Box.Parent := Form;
    Box.Left := ScaleX(16);
    Box.Top := ScaleY(84);
    Box.Width := Form.ClientWidth - ScaleX(32);
    Box.Height := ScaleY(20);
    Box.Caption := CustomMessage('RemoveDataBox') + ' (' + ExpandConstant('{localappdata}\tvsign') + ')';
    Box.Checked := False;

    Ok := TNewButton.Create(Form);
    Ok.Parent := Form;
    Ok.Width := ScaleX(100);
    Ok.Height := ScaleY(26);
    Ok.Left := Form.ClientWidth - ScaleX(16 + 100 + 8 + 100);
    Ok.Top := Form.ClientHeight - ScaleY(26 + 14);
    Ok.Caption := CustomMessage('Continue');
    Ok.ModalResult := mrOk;
    Ok.Default := True;

    Cancel := TNewButton.Create(Form);
    Cancel.Parent := Form;
    Cancel.Width := ScaleX(100);
    Cancel.Height := ScaleY(26);
    Cancel.Left := Form.ClientWidth - ScaleX(16 + 100);
    Cancel.Top := Ok.Top;
    Cancel.Caption := CustomMessage('Cancel');
    Cancel.ModalResult := mrCancel;
    Cancel.Cancel := True;

    Form.ActiveControl := Ok;
    if Form.ShowModal() <> mrOk then
      Result := False
    else
      RemoveData := Box.Checked;
  finally
    Form.Free();
  end;
  if Result then
    QuitRunningApp();
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Code: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    { what the app itself can create after installation: the autostart entry (also switched on from the
      tray menu) and the optional wake-up task }
    RegDeleteValue(HKCU, RunKey, '{#AppName}');
    RegDeleteValue(HKCU, ApprovedKey, '{#AppName}');
    Exec(ExpandConstant('{sys}\schtasks.exe'), '/Delete /TN "' + WakeTask + '" /F', '', SW_HIDE, ewWaitUntilTerminated, Code);
    if not UninstallSilent() then
      RemoveFirewallRule();
  end;
  if (CurUninstallStep = usPostUninstall) and RemoveData then
    DelTree(ExpandConstant('{localappdata}\tvsign'), True, True, True);
end;
