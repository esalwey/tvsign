# tvsign pour Windows

tvsign installe des apps sur votre Apple TV avec un Apple ID gratuit, puis les **renouvelle tout seul** avant qu'elles n'expirent. Avec un compte gratuit, une app installée de cette façon ne fonctionne que 7 jours. tvsign la signe à nouveau et la réinstalle automatiquement avant l'échéance.

Sous Windows, tvsign est une petite application avec sa propre fenêtre et une icône près de l'horloge. Il n'y a pas de navigateur à ouvrir ni de serveur à configurer.

---

## 1. Installer

1. Lancez **`tvsign-setup-<version>.exe`** (le numéro de version figure dans le nom du fichier téléchargé).
2. Si Windows affiche **« Windows a protégé votre ordinateur »**, cliquez sur **Informations complémentaires**, puis sur **Exécuter quand même**. Ce message apparaît parce que l'application n'est pas encore signée numériquement (voir la [FAQ](#7-questions-fréquentes)).
3. Laissez cochée la case **« Démarrer tvsign avec Windows »**. Elle est recommandée, car c'est ce qui permet les renouvellements automatiques.
4. Cliquez sur **Installer**, puis sur **Terminer**. tvsign s'ouvre.

L'installation se fait pour votre seul compte Windows, dans `%LOCALAPPDATA%\Programs\tvsign`. Aucun mot de passe administrateur n'est demandé.

## 2. Premier lancement : l'assistant

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

## 3. Appairer l'Apple TV

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

## 4. L'invite du pare-feu Windows

Au premier appairage, Windows peut afficher une fenêtre **« Le Pare-feu Windows Defender a bloqué certaines fonctionnalités de cette application »** au sujet de **plumesign**.

- **plumesign** est l'outil fourni avec tvsign qui parle à l'Apple TV et aux serveurs d'Apple.
- Il doit **écouter le réseau local** pour trouver l'Apple TV, qui s'annonce sur le réseau comme une imprimante ou une enceinte AirPlay.
- Cochez **Réseaux privés** et cliquez sur **Autoriser**. Windows peut demander une confirmation administrateur pour cette seule étape.
- Si vous cliquez sur **Annuler**, Windows **bloque** la recherche d'Apple TV. Pour corriger : *Paramètres Windows > Confidentialité et sécurité > Sécurité Windows > Pare-feu et protection du réseau > Autoriser une application via le pare-feu*, puis cochez **plumesign** pour « Privé ».

Vérifiez aussi que votre réseau domestique est en profil **Privé** et non **Public** (*Paramètres > Réseau et Internet > Wi-Fi ou Ethernet > Type de profil réseau*). En profil Public, Windows bloque la recherche des appareils.

La page **Diagnostic** de tvsign indique combien d'Apple TV sont visibles sur le réseau.

## 5. Le PC doit être allumé pour les renouvellements

Une app signée avec un compte gratuit expire au bout de **7 jours**. tvsign la renouvelle **60 heures avant** l'expiration, ce qui laisse environ deux jours et demi de marge.

Concrètement :
- tvsign tourne **en arrière-plan**, avec une icône près de l'horloge (dans la flèche ^ si elle est masquée).
- **Fermer la fenêtre ne quitte pas tvsign** : il continue à veiller sur vos apps.
- Le PC doit être **allumé et réveillé** : en veille ou en hibernation, tvsign ne tourne pas et ne renouvelle rien. Il doit aussi être connecté, et l'Apple TV **allumée ou en veille** sur le même réseau, au moins une fois pendant cette fenêtre de 60 heures. Si le PC se met en veille tout seul, réglez *Paramètres Windows > Système > Alimentation > Mise en veille* sur « Jamais » (l'écran peut, lui, s'éteindre).
- Si le PC reste éteint plus de deux jours et demi d'affilée au mauvais moment, l'app peut expirer sur la TV. Elle ne s'ouvre alors plus. Rallumez le PC : tvsign la renouvelle et la réinstalle à la première occasion.
- La marge peut être modifiée dans **Réglages > Renouveler avant expiration**.

Pour être prévenu en cas d'échec, tvsign affiche une notification Windows (pas pour une panne banale loin de l'expiration, comme une Apple TV en veille : l'alerte vient quand il reste moins de 24 h). Vous pouvez aussi ajouter des destinations dans **Réglages > Notifications** pour recevoir l'alerte sur votre téléphone : ntfy, Discord, Telegram, Gotify ou un webhook, chacune avec ses événements et un bouton « Envoyer un test ».

En cas de problème à signaler, **Réglages > Diagnostic > Assistance** télécharge un dossier de support dont les adresses e-mail, adresses IP et secrets sont masqués ; le journal du programme est dans `%LOCALAPPDATA%\tvsign\logs\tvsign.log`.

## 6. L'icône près de l'horloge

Clic droit sur l'icône tvsign :

| Menu | Effet |
|---|---|
| **Ouvrir** | Affiche la fenêtre (un clic sur l'icône fait de même). |
| **Renouveler maintenant** | Relance tout de suite la signature et l'installation de vos apps. |
| **Démarrer avec Windows** | Coché : tvsign se lance discrètement à l'ouverture de session. Gardez-le coché. |
| **Quitter** | Arrête tvsign. **Plus aucun renouvellement** n'a lieu tant qu'il n'est pas relancé. |

Les libellés suivent la langue choisie dans tvsign.

## 7. Questions fréquentes

**Pourquoi Windows dit-il « Windows a protégé votre ordinateur » ?**
L'installeur n'est pas encore signé avec un certificat de développeur. Le fichier n'est pas dangereux pour autant, mais Windows ne le connaît pas encore. Cliquez sur *Informations complémentaires*, puis sur *Exécuter quand même*.

**Où sont mes données ?**
Dans `%LOCALAPPDATA%\tvsign` : session Apple, appairages, liste des apps et journal (`logs\tvsign.log`). Pour les garder en lieu sûr ou changer de PC, utilisez **Réglages > Sauvegarde > Exporter une sauvegarde**.

**Comment désinstaller ?**
*Paramètres Windows > Applications > Applications installées > tvsign > Désinstaller.* Vos données sont **conservées** pour une réinstallation, sauf si vous cochez « Supprimer aussi mes données tvsign ».

**J'avais déjà utilisé Plume Impactor sur ce PC.**
tvsign ne reprend pas la session ni les appairages de Plume Impactor : reconnectez votre Apple ID et appairez de nouveau l'Apple TV dans l'assistant. (Option avancée : lancer tvsign avec la variable d'environnement `TVSIGN_IMPORT_PLUME=1` copie au premier lancement la session et les appairages existants. Ne le faites pas si Plume Impactor ou un autre tvsign utilise encore le même Apple ID.)

**Combien d'apps puis-je installer ?**
Avec un Apple ID gratuit : 3 apps actives au plus, et 10 nouveaux identifiants d'app par semaine. Ce sont des limites d'Apple.

**Quelque chose ne marche pas.**
Ouvrez la page **Diagnostic** de tvsign. Elle vérifie chaque élément et indique quoi faire. Le journal détaillé se trouve dans `%LOCALAPPDATA%\tvsign\logs\tvsign.log`.

---

## Pour les développeurs : construire l'application

Prérequis : Python 3.13 (ou 3.14) avec le lanceur `py`, et `plumesign.exe` compilé dans `..\..\Impactor\target\release\` (ou le chemin indiqué par `TVSIGN_PLUMESIGN_EXE`).

```powershell
cd tvsign\windows
powershell -ExecutionPolicy Bypass -File build.ps1          # application + installeur
powershell -ExecutionPolicy Bypass -File build.ps1 -SkipInstaller
powershell -ExecutionPolicy Bypass -File build.ps1 -Clean   # recrée l'environnement Python
```

- `dist\tvsign\` : application PyInstaller en mode *onedir* (`tvsign.exe`, `plumesign.exe`, `_internal\`).
- `dist\installer\tvsign-setup-<version>.exe` : installeur Inno Setup par utilisateur. Inno Setup est installé via `winget` s'il manque.
- La version vient de `version.txt`.

Arguments de `tvsign.exe` :
- `--tray` : démarre sans fenêtre (utilisé par le démarrage automatique) ;
- `--quit` : arrête l'instance en cours (utilisé par l'installeur) ;
- `--port N` : choisit le port local (sinon 8765 s'il est libre, ou un port au hasard).

En développement, sans compiler : `.venv-build\Scripts\python.exe launcher\tvsign_app.py`. Variables utiles : `TVSIGN_DATA` (dossier de données de test), `TVSIGN_IMPORT_PLUME=0`, `TVSIGN_WEBVIEW_DEBUG=1` (outils de développement de la fenêtre).
