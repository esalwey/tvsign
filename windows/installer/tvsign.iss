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

[Tasks]
Name: "autostart"; Description: "{cm:AutoStart}"; GroupDescription: "{cm:Options}"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; a previous version's runtime is replaced as a whole
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#AppName}"; ValueData: """{app}\{#AppExe}"" --tray"; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchNow}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Code]
var
  RemoveData: Boolean;

{ Ask a running tvsign to exit cleanly (waits up to 20 s; stops plumesign too). }
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
begin
  if (CurUninstallStep = usPostUninstall) and RemoveData then
    DelTree(ExpandConstant('{localappdata}\tvsign'), True, True, True);
end;
