# tvsign for Windows · tvsign pour Windows

[English](#english) · [Français](#français)

---

## English

tvsign installs apps on your Apple TV with a free Apple ID, then **renews them by itself** before they expire. With a free account, an app installed this way only works for 7 days. tvsign signs it again and reinstalls it automatically before the deadline.

On Windows, tvsign is a small application with its own window and an icon next to the clock. There is no browser to open and no server to set up.

### 1. Install

1. Download **`tvsign-setup-<version>.exe`** from the [releases page](https://github.com/esalwey/tvsign/releases) and, if you like, check it (see [Check your download](#check-your-download)).
2. Run it. If Windows shows **"Windows protected your PC"**, click **More info**, then **Run anyway**. This happens because the installer is not code-signed yet. If there is no "Run anyway" button, read [Windows blocks the installer](#windows-blocks-the-installer-smart-app-control-antivirus).
3. Leave **"Start tvsign with Windows"** ticked. It is recommended: it is what makes automatic renewals possible.
4. Leave **"Allow plumesign through Windows Firewall"** ticked. Windows asks you for an administrator confirmation right now, which is the only time (see [section 4](#4-the-windows-firewall)).
5. Click **Install**, then **Finish**. tvsign opens.

Installation is for your Windows account only, in `%LOCALAPPDATA%\Programs\tvsign`. No administrator password is needed, except for the optional firewall rule in step 4.

To **update**, run the new installer: it replaces the application and keeps your apps, Apple TV pairings and settings. tvsign also tells you when a new version is out (a notice in the window and a notification near the clock, once per version; it can be switched off in Settings › New versions).

#### Check your download

Each release publishes `SHA256SUMS.txt`. In PowerShell, in the download folder:

```powershell
Get-FileHash .\tvsign-setup-1.0.0.exe -Algorithm SHA256
```

The hash must be the one listed for that file in `SHA256SUMS.txt`. If it differs, do not run the file.

#### Windows blocks the installer (Smart App Control, antivirus)

The installer, `tvsign.exe` and `plumesign.exe` are not code-signed yet, so Windows has three ways to react:

- **SmartScreen** ("Windows protected your PC"): *More info* › *Run anyway*. Or right-click the file › *Properties* › tick *Unblock* › OK.
- **Smart App Control** (Windows 11 22H2 and later, on by default on a fresh install): it **blocks unsigned programs with no "Run anyway" option**. Check *Settings › Privacy & security › Windows Security › App & browser control › Smart App Control settings*. If it is **On**, the installer cannot run. You can turn it **Off**, but this is **permanent**: Windows can only enable it again after a reset or reinstall. If you do not want that, run the [Docker edition](../README.md) on a NAS, a Raspberry Pi or another computer instead.
- **Microsoft Defender or another antivirus** may flag or quarantine the program: it is built with PyInstaller, whose executables are often reported by mistake. Check the SHA-256 hash first, then allow it (*Windows Security › Virus & threat protection › Protection history › Allow on device*), or add `%LOCALAPPDATA%\Programs\tvsign` to the exclusions. You can report the false positive to Microsoft at https://www.microsoft.com/wdsi/filesubmission.

The source code of everything in the installer is in this repository, and `docs/building.md` explains how to build it yourself.

If the PC has no Microsoft Edge **WebView2** runtime (rare: stripped or old Windows 10 images), the installer says so and offers its download page. Without it tvsign still runs near the clock, and *Open in browser* in the icon menu shows the interface in your browser.

### 2. First launch: the wizard

On first launch, a six-step wizard guides you.

| Step | What to do |
|---|---|
| **Welcome** | Choose the language (French or English). If you already had tvsign on another machine, you can also **restore a backup** here. |
| **Check** | tvsign runs a few tests: Internet, Apple servers, PC clock, disk space, etc. Nothing is sent to the Apple TV. A red dot shows what is blocking, with the fix. |
| **Apple account** | Enter an Apple ID and its password, then the two-factor code received on your iPhone or Mac. **Tip: use a dedicated Apple ID**, not your main account. The password is not kept. |
| **Apple TV** | Pair your Apple TV (see section 3 below). |
| **App** | Pick the app to install. tvsign downloads it, signs it and installs it on the TV. New versions will be installed automatically. |
| **Done** | Everything is ready. |

By default the application asks for no password: it can only be reached from this PC, but by everyone who uses it (other Windows accounts, remote sessions). If the PC is shared, set a password in **Settings**.

### 3. Pair the Apple TV

Pairing is done **only once**. The PC and the Apple TV must be on **the same network** (same router, Wi-Fi or cable).

1. Turn the Apple TV on.
2. On the Apple TV, open **Settings › Remotes and Devices › Remote App and Devices**. Depending on the tvOS version the last line may be named differently.
3. **Stay on that screen.** The Apple TV only accepts a new device while it is displayed.
4. In tvsign, click **Pair**, then **Search**. Your Apple TV appears in the list. Select it.
5. A **code** appears on the TV. Type it in tvsign.
6. Done. The Apple TV shows as "paired".

If the Apple TV does not appear:
- check that the screen of step 2 is open on the TV;
- check that the PC is not on a VPN or on the router's "guest" Wi-Fi;
- see section 4 about the firewall.

If the TV is ever reset, or if you remove "tvsign" from its device list, tvsign shows "Pairing lost". Simply redo these steps.

### 4. The Windows firewall

tvsign's helper, **plumesign**, finds the Apple TV by listening on the local network (the Apple TV announces itself like a printer or an AirPlay speaker). Windows blocks that unless it is allowed.

- The installer offers to create the rule for you (a ticked option, see step 4 of section 1): **inbound, `plumesign.exe` only, private and domain networks**. Windows asks for an administrator confirmation at that moment, so nothing comes as a surprise later. In a silent installation (`/VERYSILENT`) no rule is created.
- If you declined it, Windows may show **"Windows Defender Firewall has blocked some features of this app"** about **plumesign** at the first pairing. Tick **Private networks** and click **Allow access**.
- If you clicked **Cancel**, Windows **blocks** the Apple TV search. To fix it, in an administrator PowerShell:

  ```powershell
  netsh advfirewall firewall add rule name="tvsign plumesign" dir=in action=allow program="$env:LOCALAPPDATA\Programs\tvsign\plumesign.exe" profile=private,domain enable=yes
  ```

  or *Windows Settings › Privacy & security › Windows Security › Firewall & network protection › Allow an app through firewall*, then tick **plumesign** for "Private".

Also check that your home network is set to the **Private** profile, not **Public** (*Settings › Network & internet › Wi-Fi or Ethernet › Network profile type*). On a Public profile, Windows blocks device discovery.

The tvsign **Diagnostics** page shows how many Apple TVs are visible on the network. Uninstalling tvsign removes the rule (Windows asks for a confirmation).

### 5. The PC must be on for renewals

An app signed with a free account expires after **7 days**. tvsign renews it **60 hours before** it expires, which leaves about two and a half days of margin.

In practice:
- tvsign runs **in the background**, with an icon next to the clock (in the ^ arrow when hidden).
- **Closing the window does not quit tvsign**: it keeps watching your apps.
- The PC must be **on and awake** around the renewal time. While a renewal is running, and for the half hour before one is planned, tvsign asks Windows **not to put the PC to sleep** by itself. That protects an installation from being cut short, but it cannot wake a PC that is already asleep, and it does not stop a sleep you ask for yourself (Start menu, lid closed, power button).
- If the PC sleeps or hibernates **during the 60-hour window**, nothing is renewed until it wakes up. When tvsign notices it was asleep, it writes it in its journal ("No check for 8 h 12 min: the PC was probably asleep") and renews at once. The Apple TV must also be on or asleep, on the same network, at least once in that window.
- **Optional wake-up.** Right-click the icon › **Wake the PC for renewals**: tvsign then keeps one Task Scheduler task ("tvsign wake", your account, no administrator rights) set 5 minutes before the next renewal, and it moves it after each renewal. Limits: Windows must allow wake timers (*Power Options › Change plan settings › Advanced › Sleep › Allow wake timers*), many laptops in "Modern Standby" ignore them on battery, and nothing wakes a PC that is shut down or whose lid is closed on some models. Treat it as a help, not as a guarantee. Quitting tvsign removes the task.
- The simplest and most reliable setup remains a PC that does not sleep: *Settings › System › Power & battery › Screen, sleep & hibernate timeouts* (Windows 11) or *Settings › System › Power & sleep* (Windows 10), and set "put my device to sleep after" to **Never** when plugged in (the screen may turn off).
- If the PC stays off more than two and a half days in a row at the wrong time, the app may expire on the TV and will no longer open. Turn the PC on again: tvsign renews and reinstalls it at the first opportunity.
- The margin can be changed in **Settings › Renew before expiry**.

**Restarts and the sign-in screen.** The automatic start belongs to your Windows session: it runs when you **sign in**, not when the PC boots. A PC that restarts by itself for a Windows update stops at the sign-in screen, and tvsign does not run until someone signs in. If the PC is unattended for days (holiday), either set *Windows Update › Advanced options › Active hours* and *Restart notifications* so that updates restart it while you are there, or enable automatic sign-in (Win+R › `netplwiz` › untick "Users must enter a user name and password", only on a PC in a safe place), or keep the Docker edition on an always-on machine.

To be told about a failure, tvsign shows a Windows notification, and only for real problems: an Apple TV that is simply off or asleep is not reported until the app is less than 24 hours from expiry, like the ntfy alerts. You can also add an **ntfy** address in Settings to receive alerts on your phone.

### 6. The icon near the clock

Right-click the tvsign icon:

| Menu | Effect |
|---|---|
| **Open** | Shows the window (a click on the icon does the same). |
| **Open in browser** | Shows the interface in your default browser (works without WebView2). |
| **Renew now** | Signs and installs your apps again right away. |
| **New version x.y.z: download** | Only when a newer release exists. Opens its page. |
| **Start with Windows** | Ticked: tvsign starts quietly when you sign in. Keep it ticked. If you switched it off in Task Manager or Windows Settings, the menu says so and opens *Settings › Apps › Startup* for you. |
| **Wake the PC for renewals** | Optional wake-up task, see section 5. |
| **Quit** | Stops tvsign. **No renewal happens** until it is started again. A renewal in progress gets up to 4 minutes to finish first (the icon says so). |

Labels follow the language chosen in tvsign.

### 7. Frequently asked questions

**Why does Windows say "Windows protected your PC"?**
The installer is not signed with a developer certificate yet. The file is not dangerous, but Windows does not know it yet. See [Windows blocks the installer](#windows-blocks-the-installer-smart-app-control-antivirus).

**Where is my data?**
In `%LOCALAPPDATA%\tvsign`: Apple session, pairings, app list and the log (`logs\tvsign.log`, kept under 2 MB per file with two older copies). To keep it safe or to change PC, use **Settings › Backup › Export a backup**.

**How do I uninstall?**
*Windows Settings › Apps › Installed apps › tvsign › Uninstall.* Your data is **kept** for a later reinstall unless you tick "Also delete my tvsign data". The firewall rule, the autostart entry and the wake-up task are removed.

**I already used Plume Impactor on this PC.**
tvsign does not take over Plume Impactor's session or pairings: sign in to your Apple ID again and pair the Apple TV again in the wizard. (Advanced option: starting tvsign with the environment variable `TVSIGN_IMPORT_PLUME=1` copies the existing session and pairings at first launch. Do not do this if Plume Impactor or another tvsign still uses the same Apple ID.)

**How many apps can I install?**
With a free Apple ID: at most 3 active apps, and 10 new app identifiers per week. These are Apple's limits.

**Something does not work.**
Open tvsign's **Diagnostics** page. It checks each element and says what to do. The detailed log is in `%LOCALAPPDATA%\tvsign\logs\tvsign.log`.

---

### For developers: build the application

Requirements: Python 3.13 (or 3.14) with the `py` launcher, Rust (MSVC) to build `plumesign.exe`, and Inno Setup (installed with `winget` if missing).

```powershell
cd vendor\Impactor
cargo build --release --locked -p plumesign          # -> vendor\Impactor\target\release\plumesign.exe
cd ..\..\windows
powershell -ExecutionPolicy Bypass -File build.ps1          # application + installer
powershell -ExecutionPolicy Bypass -File build.ps1 -SkipInstaller
powershell -ExecutionPolicy Bypass -File build.ps1 -Clean   # recreates the Python environment
```

- `plumesign.exe` is taken from `vendor\Impactor\target\release\` (or `TVSIGN_PLUMESIGN_EXE`).
- The build installs `requirements-lock.txt` with `--require-hashes`, so a release never depends on whatever was published since. After changing `..\requirements.txt` or `requirements-windows.txt`, regenerate it: `uv pip compile ..\requirements.txt requirements-windows.txt --python-version 3.13 --python-platform windows --generate-hashes --no-header --no-annotate -o requirements-lock.txt` (add `-c requirements-lock.txt` to keep the current versions).
- `plumesign.exe` imports `VCRUNTIME140.dll`; Windows looks for it beside `plumesign.exe` (not in `_internal\`). `tvsign.spec` copies the Visual C++ runtime DLLs it imports next to it, and `tools\pe_deps.py check dist\tvsign\plumesign.exe` verifies it. (Linking the CRT statically with `+crt-static` does not link with the current dependencies.)
- `dist\tvsign\`: PyInstaller *onedir* application (`tvsign.exe`, `plumesign.exe`, its runtime DLLs, `_internal\`).
- `dist\installer\tvsign-setup-<version>.exe`: per-user Inno Setup installer. The version comes from `version.txt`.
- `tools\smoke.ps1 -Setup dist\installer\tvsign-setup-<version>.exe`: silent install in a temporary folder, runs `plumesign.exe --version` with a bare `PATH`, starts `tvsign.exe`, checks `/api/livez`, quits, uninstalls. CI runs it on every build, after the unit tests (`python -m unittest discover -s tests`).

Arguments of `tvsign.exe`:
- `--tray`: start without a window (used by autostart);
- `--quit`: stop the running instance (used by the installer; waits for a renewal in progress);
- `--port N`: choose the local port (otherwise 8765 if free, or a random one).

During development, without building: `.venv-build\Scripts\python.exe launcher\tvsign_app.py`. Useful variables: `TVSIGN_DATA` (test data folder), `TVSIGN_IMPORT_PLUME=0`, `TVSIGN_WEBVIEW_DEBUG=1` (window developer tools), `TVSIGN_NO_DIALOGS=1` (never show a message box, for CI), `TVSIGN_SHUTDOWN_DRAIN_SECONDS` (time given to a renewal in progress when quitting, 240).

---

## Français

tvsign installe des apps sur votre Apple TV avec un Apple ID gratuit, puis les **renouvelle tout seul** avant qu'elles n'expirent. Avec un compte gratuit, une app installée de cette façon ne fonctionne que 7 jours. tvsign la signe à nouveau et la réinstalle automatiquement avant l'échéance.

Sous Windows, tvsign est une petite application avec sa propre fenêtre et une icône près de l'horloge. Il n'y a pas de navigateur à ouvrir ni de serveur à configurer.

### 1. Installer

1. Téléchargez **`tvsign-setup-<version>.exe`** depuis la [page des versions](https://github.com/esalwey/tvsign/releases) et, si vous le souhaitez, vérifiez-le (voir [Vérifier le téléchargement](#vérifier-le-téléchargement)).
2. Lancez-le. Si Windows affiche **« Windows a protégé votre ordinateur »**, cliquez sur **Informations complémentaires**, puis sur **Exécuter quand même**. Ce message apparaît parce que l'installeur n'est pas encore signé numériquement. S'il n'y a pas de bouton « Exécuter quand même », lisez [Windows bloque l'installeur](#windows-bloque-linstalleur-smart-app-control-antivirus).
3. Laissez cochée la case **« Démarrer tvsign avec Windows »**. Elle est recommandée, car c'est ce qui permet les renouvellements automatiques.
4. Laissez cochée la case **« Autoriser plumesign dans le Pare-feu Windows »**. Windows demande une confirmation administrateur tout de suite, et c'est la seule fois (voir la [section 4](#4-le-pare-feu-windows)).
5. Cliquez sur **Installer**, puis sur **Terminer**. tvsign s'ouvre.

L'installation se fait pour votre seul compte Windows, dans `%LOCALAPPDATA%\Programs\tvsign`. Aucun mot de passe administrateur n'est demandé, sauf pour la règle de pare-feu facultative de l'étape 4.

Pour **mettre à jour**, lancez le nouvel installeur : il remplace l'application et conserve vos apps, vos appairages Apple TV et vos réglages. tvsign vous prévient aussi quand une nouvelle version sort (un bandeau dans la fenêtre et une notification près de l'horloge, une fois par version ; désactivable dans Réglages › Nouvelles versions).

#### Vérifier le téléchargement

Chaque version publie `SHA256SUMS.txt`. Dans PowerShell, dans le dossier du téléchargement :

```powershell
Get-FileHash .\tvsign-setup-1.0.0.exe -Algorithm SHA256
```

L'empreinte doit être celle de ce fichier dans `SHA256SUMS.txt`. Si elle diffère, ne lancez pas le fichier.

#### Windows bloque l'installeur (Smart App Control, antivirus)

L'installeur, `tvsign.exe` et `plumesign.exe` ne sont pas encore signés numériquement. Windows peut donc réagir de trois façons :

- **SmartScreen** (« Windows a protégé votre ordinateur ») : *Informations complémentaires* › *Exécuter quand même*. Ou clic droit sur le fichier › *Propriétés* › cochez *Débloquer* › OK.
- **Smart App Control** (Windows 11 22H2 et suivants, activé par défaut sur une installation neuve) : il **bloque les programmes non signés, sans bouton « Exécuter quand même »**. Regardez *Paramètres › Confidentialité et sécurité › Sécurité Windows › Contrôle des applications et du navigateur › Paramètres de Smart App Control*. S'il est **activé**, l'installeur ne peut pas s'exécuter. Vous pouvez le **désactiver**, mais c'est **définitif** : Windows ne peut le réactiver qu'après une réinitialisation ou une réinstallation. Si vous ne le souhaitez pas, utilisez plutôt l'[édition Docker](../README.md) sur un NAS, un Raspberry Pi ou un autre ordinateur.
- **Microsoft Defender ou un autre antivirus** peut signaler ou mettre en quarantaine le programme : il est fabriqué avec PyInstaller, dont les exécutables sont souvent signalés à tort. Vérifiez d'abord l'empreinte SHA-256, puis autorisez-le (*Sécurité Windows › Protection contre les virus et menaces › Historique de protection › Autoriser sur l'appareil*), ou ajoutez `%LOCALAPPDATA%\Programs\tvsign` aux exclusions. Vous pouvez signaler le faux positif à Microsoft : https://www.microsoft.com/wdsi/filesubmission.

Le code source de tout ce que contient l'installeur est dans ce dépôt, et `docs/building.md` explique comment le compiler soi-même.

Si le PC n'a pas le composant **WebView2** de Microsoft Edge (rare : images Windows 10 allégées ou anciennes), l'installeur le signale et propose sa page de téléchargement. Sans lui, tvsign fonctionne quand même près de l'horloge, et *Ouvrir dans le navigateur* dans le menu de l'icône affiche l'interface dans votre navigateur.

### 2. Premier lancement : l'assistant

Au premier lancement, un assistant en six étapes vous guide.

| Étape | Ce qu'il faut faire |
|---|---|
| **Bienvenue** | Choisissez la langue (français ou anglais). Si vous aviez déjà tvsign sur une autre machine, vous pouvez aussi **restaurer une sauvegarde** ici. |
| **Vérification** | tvsign fait quelques tests : accès à Internet, aux serveurs d'Apple, heure du PC, espace disque, etc. Rien n'est envoyé à l'Apple TV. Un point rouge indique ce qui bloque, avec la solution. |
| **Compte Apple** | Saisissez un Apple ID et son mot de passe, puis le code à deux facteurs reçu sur votre iPhone ou votre Mac. **Conseil : utilisez un Apple ID dédié**, pas votre compte principal. Le mot de passe n'est pas conservé. |
| **Apple TV** | Appairez votre Apple TV (voir la section 3 ci-dessous). |
| **App** | Choisissez l'app à installer. tvsign la télécharge, la signe et l'installe sur la TV. Les nouvelles versions seront installées automatiquement. |
| **Terminé** | Tout est prêt. |

Par défaut, l'application ne demande pas de mot de passe : elle n'est joignable que depuis ce PC, mais par tous ceux qui l'utilisent (autres comptes Windows, session à distance). Si le PC est partagé, définissez un mot de passe dans **Réglages**.

### 3. Appairer l'Apple TV

L'appairage ne se fait **qu'une seule fois**. Le PC et l'Apple TV doivent être sur **le même réseau** (même box, Wi-Fi ou câble).

1. Allumez l'Apple TV.
2. Sur l'Apple TV, ouvrez **Réglages > Télécommandes et appareils > App Distante**. Selon la version de tvOS, la dernière ligne peut s'appeler « App Télécommande et appareils ».
3. **Restez sur cet écran.** L'Apple TV n'accepte un nouvel appareil que tant que cet écran est affiché.
4. Dans tvsign, cliquez sur **Appairer**, puis sur **Rechercher**. Votre Apple TV apparaît dans la liste. Sélectionnez-la.
5. Un **code** s'affiche sur la TV. Saisissez-le dans tvsign.
6. C'est fait. L'Apple TV apparaît comme « appairée ».

Si l'Apple TV n'apparaît pas :
- vérifiez que l'écran de l'étape 2 est bien ouvert sur la TV ;
- vérifiez que le PC n'est pas connecté à un VPN ni au Wi-Fi « invité » de la box ;
- voyez la section 4 sur le pare-feu.

Si un jour la TV est réinitialisée, ou si vous retirez « tvsign » de la liste des appareils dans ses réglages, tvsign affichera « Appairage perdu ». Il suffira alors de refaire ces étapes.

### 4. Le pare-feu Windows

L'outil **plumesign**, fourni avec tvsign, trouve l'Apple TV en **écoutant le réseau local** (l'Apple TV s'annonce sur le réseau comme une imprimante ou une enceinte AirPlay). Windows bloque cette écoute tant qu'elle n'est pas autorisée.

- L'installeur propose de créer la règle à votre place (option cochée, voir l'étape 4 de la section 1) : **entrant, `plumesign.exe` uniquement, réseaux privés et de domaine**. Windows demande une confirmation administrateur à ce moment-là, donc aucune surprise plus tard. Dans une installation silencieuse (`/VERYSILENT`), aucune règle n'est créée.
- Si vous l'avez refusée, Windows peut afficher, au premier appairage, **« Le Pare-feu Windows Defender a bloqué certaines fonctionnalités de cette application »** au sujet de **plumesign**. Cochez **Réseaux privés** et cliquez sur **Autoriser**.
- Si vous avez cliqué sur **Annuler**, Windows **bloque** la recherche d'Apple TV. Pour corriger, dans un PowerShell administrateur :

  ```powershell
  netsh advfirewall firewall add rule name="tvsign plumesign" dir=in action=allow program="$env:LOCALAPPDATA\Programs\tvsign\plumesign.exe" profile=private,domain enable=yes
  ```

  ou *Paramètres Windows › Confidentialité et sécurité › Sécurité Windows › Pare-feu et protection du réseau › Autoriser une application via le pare-feu*, puis cochez **plumesign** pour « Privé ».

Vérifiez aussi que votre réseau domestique est en profil **Privé** et non **Public** (*Paramètres › Réseau et Internet › Wi-Fi ou Ethernet › Type de profil réseau*). En profil Public, Windows bloque la recherche des appareils.

La page **Diagnostic** de tvsign indique combien d'Apple TV sont visibles sur le réseau. Désinstaller tvsign supprime la règle (Windows demande une confirmation).

### 5. Le PC doit être allumé pour les renouvellements

Une app signée avec un compte gratuit expire au bout de **7 jours**. tvsign la renouvelle **60 heures avant** l'expiration, ce qui laisse environ deux jours et demi de marge.

Concrètement :
- tvsign tourne **en arrière-plan**, avec une icône près de l'horloge (dans la flèche ^ si elle est masquée).
- **Fermer la fenêtre ne quitte pas tvsign** : il continue à veiller sur vos apps.
- Le PC doit être **allumé et réveillé** au moment du renouvellement. Pendant un renouvellement, et la demi-heure qui précède un renouvellement prévu, tvsign demande à Windows de **ne pas mettre le PC en veille** de lui-même. Cela évite qu'une installation soit coupée en route, mais cela ne peut pas réveiller un PC déjà endormi, ni empêcher une veille que vous demandez vous-même (menu Démarrer, capot refermé, bouton d'alimentation).
- Si le PC se met en veille ou en hibernation **pendant la fenêtre de 60 heures**, rien n'est renouvelé avant son réveil. Quand tvsign constate qu'il a dormi, il l'écrit dans son journal (« Aucune vérification depuis 8 h 12 min : le PC était sans doute en veille ») et renouvelle aussitôt. L'Apple TV doit aussi être **allumée ou en veille**, sur le même réseau, au moins une fois pendant cette fenêtre.
- **Réveil facultatif.** Clic droit sur l'icône › **Réveiller le PC pour les renouvellements** : tvsign garde alors une tâche du Planificateur de tâches (« tvsign wake », votre compte, sans droits administrateur) placée 5 minutes avant le prochain renouvellement, et la déplace après chaque renouvellement. Limites : Windows doit autoriser les minuteries de réveil (*Options d'alimentation › Modifier les paramètres du mode › Paramètres avancés › Veille › Autoriser les minuteries de réveil*), beaucoup de portables en « veille moderne » les ignorent sur batterie, et sur certains modèles rien ne réveille un PC éteint ou capot fermé. C'est une aide, pas une garantie. Quitter tvsign supprime la tâche.
- Le plus simple et le plus fiable reste un PC qui ne se met pas en veille : *Paramètres › Système › Alimentation et batterie › Écran, mise en veille et hibernation* (Windows 11) ou *Paramètres › Système › Alimentation et mise en veille* (Windows 10), puis réglez « mettre l'appareil en veille après » sur **Jamais** sur secteur (l'écran peut, lui, s'éteindre).
- Si le PC reste éteint plus de deux jours et demi d'affilée au mauvais moment, l'app peut expirer sur la TV. Elle ne s'ouvre alors plus. Rallumez le PC : tvsign la renouvelle et la réinstalle à la première occasion.
- La marge peut être modifiée dans **Réglages > Renouveler avant expiration**.

**Redémarrages et écran de connexion.** Le démarrage automatique dépend de votre session Windows : il a lieu à l'**ouverture de session**, pas au démarrage du PC. Un PC qui redémarre seul pour une mise à jour Windows s'arrête à l'écran de connexion, et tvsign ne tourne pas tant que personne ne se connecte. Si le PC reste sans surveillance plusieurs jours (vacances), réglez *Windows Update › Options avancées › Heures d'activité* et *Notifications de redémarrage* pour que les mises à jour le redémarrent quand vous êtes là, ou activez la connexion automatique (Win+R › `netplwiz` › décochez « Les utilisateurs doivent entrer un nom et un mot de passe », uniquement sur un PC placé en lieu sûr), ou gardez l'édition Docker sur une machine toujours allumée.

Pour être prévenu en cas d'échec, tvsign affiche une notification Windows, et seulement pour les vrais problèmes : une Apple TV simplement éteinte ou en veille n'est signalée que lorsque l'app est à moins de 24 heures de l'expiration, comme pour les alertes ntfy. Vous pouvez aussi ajouter une adresse **ntfy** dans les Réglages pour recevoir l'alerte sur votre téléphone.

### 6. L'icône près de l'horloge

Clic droit sur l'icône tvsign :

| Menu | Effet |
|---|---|
| **Ouvrir** | Affiche la fenêtre (un clic sur l'icône fait de même). |
| **Ouvrir dans le navigateur** | Affiche l'interface dans votre navigateur par défaut (fonctionne sans WebView2). |
| **Renouveler maintenant** | Relance tout de suite la signature et l'installation de vos apps. |
| **Nouvelle version x.y.z : télécharger** | Seulement quand une version plus récente existe. Ouvre sa page. |
| **Démarrer avec Windows** | Coché : tvsign se lance discrètement à l'ouverture de session. Gardez-le coché. Si vous l'avez désactivé dans le Gestionnaire des tâches ou les Paramètres Windows, le menu l'indique et ouvre *Paramètres › Applications › Démarrage*. |
| **Réveiller le PC pour les renouvellements** | Tâche de réveil facultative, voir la section 5. |
| **Quitter** | Arrête tvsign. **Plus aucun renouvellement** n'a lieu tant qu'il n'est pas relancé. Un renouvellement en cours dispose de 4 minutes pour se terminer (l'icône le signale). |

Les libellés suivent la langue choisie dans tvsign.

### 7. Questions fréquentes

**Pourquoi Windows dit-il « Windows a protégé votre ordinateur » ?**
L'installeur n'est pas encore signé avec un certificat de développeur. Le fichier n'est pas dangereux pour autant, mais Windows ne le connaît pas encore. Voir [Windows bloque l'installeur](#windows-bloque-linstalleur-smart-app-control-antivirus).

**Où sont mes données ?**
Dans `%LOCALAPPDATA%\tvsign` : session Apple, appairages, liste des apps et journal (`logs\tvsign.log`, limité à 2 Mo par fichier avec deux copies plus anciennes). Pour les garder en lieu sûr ou changer de PC, utilisez **Réglages > Sauvegarde > Exporter une sauvegarde**.

**Comment désinstaller ?**
*Paramètres Windows > Applications > Applications installées > tvsign > Désinstaller.* Vos données sont **conservées** pour une réinstallation, sauf si vous cochez « Supprimer aussi mes données tvsign ». La règle de pare-feu, l'entrée de démarrage automatique et la tâche de réveil sont supprimées.

**J'avais déjà utilisé Plume Impactor sur ce PC.**
tvsign ne reprend pas la session ni les appairages de Plume Impactor : reconnectez votre Apple ID et appairez de nouveau l'Apple TV dans l'assistant. (Option avancée : lancer tvsign avec la variable d'environnement `TVSIGN_IMPORT_PLUME=1` copie au premier lancement la session et les appairages existants. Ne le faites pas si Plume Impactor ou un autre tvsign utilise encore le même Apple ID.)

**Combien d'apps puis-je installer ?**
Avec un Apple ID gratuit : 3 apps actives au plus, et 10 nouveaux identifiants d'app par semaine. Ce sont des limites d'Apple.

**Quelque chose ne marche pas.**
Ouvrez la page **Diagnostic** de tvsign. Elle vérifie chaque élément et indique quoi faire. Le journal détaillé se trouve dans `%LOCALAPPDATA%\tvsign\logs\tvsign.log`.

---

### Pour les développeurs : construire l'application

Prérequis : Python 3.13 (ou 3.14) avec le lanceur `py`, Rust (MSVC) pour compiler `plumesign.exe`, et Inno Setup (installé via `winget` s'il manque).

```powershell
cd vendor\Impactor
cargo build --release --locked -p plumesign          # -> vendor\Impactor\target\release\plumesign.exe
cd ..\..\windows
powershell -ExecutionPolicy Bypass -File build.ps1          # application + installeur
powershell -ExecutionPolicy Bypass -File build.ps1 -SkipInstaller
powershell -ExecutionPolicy Bypass -File build.ps1 -Clean   # recrée l'environnement Python
```

- `plumesign.exe` est pris dans `vendor\Impactor\target\release\` (ou `TVSIGN_PLUMESIGN_EXE`).
- Le build installe `requirements-lock.txt` avec `--require-hashes` : une version publiée ne dépend pas de ce qui a été publié depuis. Après une modification de `..\requirements.txt` ou de `requirements-windows.txt`, régénérez-le : `uv pip compile ..\requirements.txt requirements-windows.txt --python-version 3.13 --python-platform windows --generate-hashes --no-header --no-annotate -o requirements-lock.txt` (ajoutez `-c requirements-lock.txt` pour garder les versions actuelles).
- `plumesign.exe` importe `VCRUNTIME140.dll` ; Windows la cherche à côté de `plumesign.exe` (pas dans `_internal\`). `tvsign.spec` copie à côté de lui les DLL du runtime Visual C++ qu'il importe, et `tools\pe_deps.py check dist\tvsign\plumesign.exe` le vérifie. (Lier la CRT statiquement avec `+crt-static` ne fonctionne pas avec les dépendances actuelles.)
- `dist\tvsign\` : application PyInstaller en mode *onedir* (`tvsign.exe`, `plumesign.exe`, ses DLL du runtime, `_internal\`).
- `dist\installer\tvsign-setup-<version>.exe` : installeur Inno Setup par utilisateur. La version vient de `version.txt`.
- `tools\smoke.ps1 -Setup dist\installer\tvsign-setup-<version>.exe` : installation silencieuse dans un dossier temporaire, `plumesign.exe --version` avec un `PATH` minimal, lancement de `tvsign.exe`, contrôle de `/api/livez`, arrêt, désinstallation. La CI l'exécute à chaque build, après les tests unitaires (`python -m unittest discover -s tests`).

Arguments de `tvsign.exe` :
- `--tray` : démarre sans fenêtre (utilisé par le démarrage automatique) ;
- `--quit` : arrête l'instance en cours (utilisé par l'installeur ; attend la fin d'un renouvellement en cours) ;
- `--port N` : choisit le port local (sinon 8765 s'il est libre, ou un port au hasard).

En développement, sans compiler : `.venv-build\Scripts\python.exe launcher\tvsign_app.py`. Variables utiles : `TVSIGN_DATA` (dossier de données de test), `TVSIGN_IMPORT_PLUME=0`, `TVSIGN_WEBVIEW_DEBUG=1` (outils de développement de la fenêtre), `TVSIGN_NO_DIALOGS=1` (n'affiche jamais de boîte de dialogue, pour la CI), `TVSIGN_SHUTDOWN_DRAIN_SECONDS` (temps laissé à un renouvellement en cours quand on quitte, 240).
