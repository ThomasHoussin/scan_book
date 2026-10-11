# Scan Book — livres sous Windows

Un script Python autonome, avec acquisition WIA, aperçu de correction et sessions
reprenables. Une page de livre par acquisition ; couleur, 300 ppp par défaut.

## Démarrage

Prérequis : Windows, [uv](https://docs.astral.sh/uv/getting-started/installation/),
le pilote WIA du scanner et un Python avec Tcl/Tk. Le Python 3.12 géré par uv sur
cette machine fournit Tcl/Tk. Le premier lancement télécharge les dépendances ;
les suivants réutilisent le cache uv. Aucun `pip install` ni environnement virtuel
à gérer manuellement, aucune dépendance au projet `search_bench`.

```powershell
cd D:\code-workspace\scan_book
uv run tools/scan_book.py devices
uv run tools/scan_book.py scan les-veilleurs
```

Poser **une page** sur la vitre. Dans la console ou la fenêtre d'aperçu :

| Touche | Action |
|---|---|
| Entrée | Scanner la page suivante, traiter et sauvegarder |
| C | Corriger la dernière page depuis son brut |
| R | Rescanner uniquement la dernière page |
| M | Basculer entre couleur et N&B (niveaux de gris) pour les prochaines acquisitions |
| F | Créer le PDF puis quitter |
| Q | Quitter sans PDF ; la session reste disponible |

Les commandes de scan sont ignorées pendant une acquisition. WIA `Transfer` ne
permet pas l'annulation en cours : attendre sa fin avant de fermer. Dans le
formulaire de correction, utiliser **Valider**, **Annuler** ou Échap ; les touches
de scan sont désactivées pour éviter un déclenchement accidentel.

Le **mode du prochain scan** reste affiché dans l'aperçu, à côté du bouton
**M · Couleur / N&B**. Pour un livre avec une couverture couleur, scanner la
couverture puis appuyer sur **M** et continuer en niveaux de gris. Appuyer à
nouveau sur **M** pour revenir en couleur. Le mode est mémorisé à la reprise ;
les pages déjà sauvegardées conservent leur couleur. Un rescan avec **R** utilise
le mode courant. Le changement est désactivé pendant une acquisition ou une correction.

## Reprise et réglages

Relancer exactement la même commande pour reprendre le livre. Les réglages de
la session sont réutilisés ; les options explicites remplacent seulement les
réglages des prochaines pages.

```powershell
uv run tools/scan_book.py scan les-veilleurs --dpi 600 --color gray
uv run tools/scan_book.py scan les-veilleurs --area 0,0,148,210
uv run tools/scan_book.py scan les-veilleurs --area full
uv run tools/scan_book.py scan les-veilleurs --scanner "EPSON Perfection"
```

`--area` est exprimé en millimètres : x, y, largeur, hauteur depuis le coin
supérieur gauche de la vitre. `full` restaure toute la vitre. Les résolutions et
zones refusées par le pilote sont signalées avant acquisition. Si plusieurs
scanners sont présents, copier l'identifiant retourné par `devices` dans
`--scanner`. Un fragment de nom unique est aussi accepté.

`--root "E:\Mes scans"` permet de choisir un autre dossier contenant les livres ;
fournir ce même dossier aux commandes suivantes. Par défaut, `scans` est résolu
par rapport au script, quel que soit le dossier courant de la console.

## Orienter, redresser et recadrer

Après chaque acquisition, le script détecte le texte à l'envers et applique une
rotation de **180°** lorsque plusieurs lignes concordent. Le détecteur local
RapidOCR et ses modèles sont installés automatiquement par `uv` ; aucune image
n'est envoyée à un service. Au moins trois lignes doivent être reconnues avec
une confiance de 90 %, au moins la moitié des lignes analysées doivent être
fiables et 80 % de ces lignes doivent indiquer le même sens. Les pages tournées
de 90°, pauvres en texte ou ambiguës restent à orienter manuellement avec **C**.
L'aperçu signale **Rotation auto 180°** lorsqu'elle est appliquée.

Le redressement recherche un contour de papier ou des lignes de texte concordantes.
Pour le recadrage, un contour rectangulaire complet conserve une marge de sécurité
de 3 mm. Si le papier touche les bords du scan, **haut, bas, gauche et droite sont
analysés indépendamment** : toute combinaison de côtés peut être rognée. Une
ombre de bord suffisamment continue, suivie du fond clair du scanner, permet de
retirer ce fond et la partie sombre de la bordure sans conserver la marge de 3 mm.
Les marges imprimées sont conservées ; il ne s'agit pas de rogner au ras du texte.

Les pages à fond sombre sur le fond clair du scanner sont aussi repérées lorsque
la page occupe la majorité de la largeur et de la hauteur et touche deux bords
adjacents du scan. Ce recadrage retire les grandes zones claires extérieures,
conserve 3 mm de sécurité et reste signalé **À VÉRIFIER**. Un rectangle sombre
entièrement à l'intérieur de l'image est conservé : il pourrait s'agir d'une
illustration imprimée. Les limites peu contrastées restent à corriger avec **C**.

Une limite invisible ou incertaine est conservée. Le script affiche **À VÉRIFIER**
si le traitement reste incertain ; une erreur du détecteur d'orientation n'empêche
pas de sauvegarder le scan. Les originaux dans `raw` restent intacts. Relancer le
script pour appliquer ces traitements aux prochaines acquisitions ; les pages
déjà sauvegardées ne sont pas retraitées automatiquement.

Avec **C**, l'aperçu montre le brut redressé et le rectangle conservé en vert :

1. Régler l'angle ; utiliser **↶ 90°** si la page est tournée.
2. Tracer à la souris le rectangle à conserver. Modifier ensuite l'angle ou
   l'orientation remet le recadrage à zéro pour éviter des coordonnées incohérentes.
3. **Valider** sauvegarde ; **Annuler** conserve la version précédente.

Le zoom et les barres de défilement permettent d'inspecter les détails.
Le fond blanc hachuré indique l'espace **hors image** ; un fin contour délimite
la page affichée. Les zones sombres à l'intérieur de ce contour font partie du
scan. En correction, le cadre vert indique la zone qui sera conservée.
**Brut / réinitialiser** retire toutes les corrections. Pour une page plus ancienne,
même scanner débranché :

```powershell
uv run tools/scan_book.py correct les-veilleurs --page 12
```

Pas de transcription OCR, de séparation de doubles pages ni de correction de courbure de reliure.
Le traitement automatique demande une vérification visuelle ; les bruts restent
disponibles pour reprendre une mauvaise détection.

## Variante DeepLabV3 (expérimentale)

Le script original reste intact. Pour reprendre un livre avec DeepLabV3 :

```powershell
uv run tools/scan_book_deeplab.py scan "Les veilleurs"
```

Attendre la fin de l'acquisition, quitter l'ancien script avec **Q**, puis lancer
la variante avec le même livre et le même `--root` éventuel. Elle utilise **les
mêmes dossiers `scans`, `raw`, `pages`, sessions et PDF**, avec le même verrou.
Les anciennes pages ne sont pas retraitées. Les commandes, touches, modes
couleur/N&B et corrections manuelles restent identiques.

DeepLabV3–ResNet50 utilise les [poids documents de LearnOpenCV](https://github.com/spmallick/learnopencv/tree/master/Document-Scanner-Custom-Semantic-Segmentation-using-PyTorch-DeepLabV3)
(`model_r50_iou_mix_2C020.pth`). Les rotations à 180° et le redressement restent
ceux du script original. Le masque donne un rectangle avec une marge extérieure
de 0,5 mm, bornée par l'image. La découpe conserve la résolution et les couleurs ;
aucune correction de perspective ou de courbure n'est appliquée.

Le premier lancement installe PyTorch CUDA via uv (environ 3 Gio pour la roue
torch). Au premier traitement, les poids (161 Mio) sont téléchargés, vérifiés
par SHA-256 et mis en cache dans `.cache/deeplab`, près du script. Ensuite,
l'inférence reste locale et hors ligne. Le modèle est chargé une fois dans le
worker d'acquisition, sur GPU CUDA s'il est disponible, sinon CPU. La console
indique le périphérique. Une récupération de brut après interruption peut
déclencher ce traitement dès l'ouverture. Sans récupération en attente,
`devices`, `correct` et `export` ne chargent pas DeepLabV3.

**Limite constatée : le modèle peut confondre la page avec ses blocs imprimés.**
Un contrôle réutilise le détecteur de texte RapidOCR pour rejeter les découpes
qui excluent du texte détecté, y compris les légendes verticales. Il ne garantit
pas la conservation des illustrations ou des textes manqués par ce détecteur.
Une segmentation ambiguë ou une erreur conserve l'image orientée/redressée sans
recadrage, signalée **À VÉRIFIER**. Aucun repli sur les anciennes heuristiques.

Contrôle du 27 septembre 2026 : RTX 5070 Ti confirmée avec PyTorch 2.7.1+cu128.
Sur quatre exemples, la page sombre est recadrée ; la couverture est rejetée
comme ambiguë et deux découpes de pages illustrées sont rejetées car elles
coupent du texte. Ces trois pages restent entières, à corriger manuellement.
Cette variante n'est **pas une amélioration générale validée** du recadrage.
Les contrôles ont utilisé des fichiers existants, sans déclencher le scanner.

```powershell
uv run tools/scan_book_deeplab.py correct "Les veilleurs" --page 23
uv run tests/test_scan_book_deeplab.py
```

Les tests ne téléchargent pas les poids et n'exigent pas de GPU ; ils couvrent
les sessions, la découpe, les échecs, l'acquisition simulée et l'interface Tk.

## Variante Qwen (expérimentale)

La troisième variante utilise **Qwen3.5 9B**, exécuté localement par Ollama :

```powershell
uv run tools/scan_book_qwen.py scan "Les veilleurs"
```

Attendre la fin de l'acquisition de l'ancien script, quitter avec **Q**, puis
reprendre le même livre avec cette commande et le même `--root` éventuel.
Les trois scripts partagent **les mêmes dossiers, sessions, PDF et verrou**.
Les pages des anciennes sessions sont considérées comme déjà validées, sans
migration ni retraitement. Les commandes et options restent identiques ; la
variante Qwen utilise désormais le parcours suivant. Les deux autres scripts
restent intacts.

### 1. Scanner en masse

**Entrée** lance le premier scan, puis accepte la capture affichée et lance le
suivant. L'aperçu montre le **brut sauvegardé**, sans attendre Qwen. Orientation,
redressement et recadrage se font dans un worker séparé, une page à la fois,
pendant que le scanner peut acquérir les pages suivantes. Les résultats ne
remplacent pas le brut affiché pendant cette phase.

**R** refait la dernière capture ; **M** change le mode du prochain scan.
Les frappes pendant une acquisition ne déclenchent pas de scans supplémentaires.
**F** termine les acquisitions, conserve la dernière capture et ouvre la
vérification. Les compteurs indiquent les pages acquises, traitées et à valider.

### 2. Vérifier les pages

Les pages nouvelles ou non validées sont présentées dans l'ordre du livre.
La vérification commence dès que la première est prête ; si la suivante attend
encore Qwen, l'écran affiche cette attente pendant que le traitement continue.

- **Entrée** valide la page affichée et passe à la suivante.
- Une image absente dans `pages/` ou `raw/` fait ignorer la page : son numéro
  est signalé dans l'interface et elle est omise du PDF. Pour retirer une page,
  quitter avec **Q**, supprimer simplement son fichier dans `pages/`, puis
  reprendre le livre. Il n'est pas nécessaire de modifier `session.json`.
  Le traitement Qwen ne recrée pas l'image supprimée. Restaurer le fichier
  permet de retrouver la page à la reprise.
- **C** ouvre l'éditeur habituel : angle, rotation, rectangle de recadrage et
  réinitialisation. **Valider** sauvegarde la correction et revient à l'aperçu ;
  **Entrée** confirme ensuite la page. **Annuler** ou **Échap** abandonne l'édition.
- Après la dernière validation : **« Vérification terminée — F pour créer le PDF »**.
  F ne permet pas d'exporter tant qu'une page reste à traiter ou à valider.

### 3. Consolider le PDF

**F** assemble toutes les pages, anciennes comprises, dans l'ordre du manifeste,
puis ferme l'interface. En cas d'échec d'export, la session reste ouverte pour
réessayer ; un PDF préexistant reste intact si l'assemblage échoue.

**Q** ou la fermeture de la fenêtre conserve la session. Une acquisition WIA
engagée termine sa sauvegarde ; le traitement Qwen est arrêté sans vider la file.
Relancer la même commande reprend les traitements restants et la phase en cours.
Une reprise en vérification ne nécessite pas de scanner. Après un export réussi,
relancer `scan` permet d'ajouter des pages sans revoir les anciennes.

Les états Qwen sont enregistrés dans des champs facultatifs du manifeste :
`qwen_workflow.phase` et, par page, `qwen` (`revision`, `processing`, `validated`).
Ils sont distincts de l'avertissement automatique `transform.review`. Les bruts
et des images provisoires non recadrées sont sauvegardés avant le traitement ;
la file en mémoire ne contient que des références de pages. Un résultat tardif
ne peut pas remplacer une capture refaite ou une correction manuelle.

**Une session Qwen en cours doit être reprise avec le script Qwen.** Les autres
variantes ne vérifient pas ces nouveaux états et pourraient exporter des images
provisoires. Les sessions terminées restent compatibles. La commande `export`
de Qwen refuse les pages en attente de traitement ou de validation et indique
comment reprendre le parcours. La commande `correct --page` reste disponible ;
une correction d'une page Qwen la remet à valider dans le parcours.

### Traitement automatique

Ollama doit être démarré sur `127.0.0.1:11434` (application Ollama, ou
`ollama serve` dans un terminal si aucun serveur ne tourne). Au premier
traitement, le script télécharge `qwen3.5:9b` s'il manque, avec progression dans
la console, dans le cache habituel d'Ollama (~6,6 Go). Le téléchargement peut
aussi être préparé avec `ollama pull qwen3.5:9b`. Le mode de raisonnement est
désactivé (`think=false`) pour obtenir directement le rectangle JSON.
Ensuite, le traitement reste
local et fonctionne hors ligne. Le script n'utilise ni proxy HTTP ni service
distant et n'installe pas PyTorch. Ollama gère le GPU et son éventuel repli CPU ;
`ollama ps` affiche la répartition. Le modèle reste chargé 30 minutes après usage.

La détection du 180° et le redressement restent identiques. Qwen reçoit une
copie RGB de l'image redressée, de 1 600 pixels maximum sur le grand côté.
La consigne est : « Recadre la page sur les bords du papier, en t'assurant de
ne couper aucun texte ni aucune image, mais en retirant les ombres, les marques
de reliure et les bordures des pages. » Ses coordonnées sont reportées
sur la pleine résolution avec arrondi extérieur et marge de **0,5 mm** selon
les DPI. L'image sauvegardée conserve ses pixels et son mode couleur/gris,
hors rotation/redressement et découpe habituels. Aucune correction de perspective.
Les nouvelles transformations portent `method="qwen3.5"`. Les transformations
antérieures `qwen3-vl` restent lisibles, sans migration ni retraitement.

Une réponse invalide ou incertaine, une zone inférieure à 20 % de l'image,
ou une erreur Ollama conserve l'image
orientée/redressée entière avec **À VÉRIFIER**. L'inférence est limitée à
180 secondes ; le téléchargement initial dispose de 30 minutes et peut être
repris après interruption. Ces opérations s'exécutent dans le worker de
traitement, indépendamment des acquisitions. Les nouveaux bruts récupérés après
interruption sont remis dans la file sans inférence à l'ouverture. Un ancien
journal de capture créé avant ce workflow conserve sa récupération historique.
Hors récupération d'un tel ancien journal, `devices`, `correct` et `export`
n'ont pas besoin d'Ollama. Le PDF ne contient toujours aucune couche de texte OCR.

Le cadre Qwen est appliqué sans contrôle OCR après la détection. La vérification
visuelle et la correction manuelle avec **C** permettent d'ajuster la découpe.
Ce changement s'applique aux prochains traitements ; les pages déjà traitées
ne sont pas recalculées automatiquement.

**Limite :** les coordonnées de Qwen sont approximatives, et son indicateur de
certitude n'est pas une mesure calibrée. Vérifier l'aperçu, surtout sur une
nouvelle mise en page.

Contrôle du 27 septembre 2026 avec **Qwen3.5-9B Q4_K_M**
(`6488c96fa5fa`) : Ollama indique **100 % GPU** sur la RTX 5070 Ti, environ
5,6 Go de mémoire pour le modèle chargé, avec un contexte de 8 192 tokens.
Sur les quatre bruts déjà utilisés pour DeepLab, la couverture est conservée
entière car le modèle répond `certain=false`. Les trois pages intérieures
sont effectivement recadrées, sans texte ni illustration coupé observé :

| Exemple | Brut | Résultat |
|---|---|---|
| Page illustrée | 2 550 × 3 510 | 2 509 × 3 358 |
| Page claire, légendes verticales | 2 550 × 3 510 | 2 405 × 3 256 |
| Page sombre | 1 829 × 2 927 | 1 749 × 2 808 |

La bande supérieure de fond de la page claire est retirée, contrairement à
Qwen3-VL. Certains cadres sont serrés près des titres et une fine ombre peut
subsister : vérifier l'aperçu. Ce contrôle sur quatre images ne garantit pas
le résultat sur d'autres mises en page. Le traitement prend environ
2,5–3,1 secondes par page une fois le modèle chargé (13,3 secondes au premier
essai, hors téléchargement).
Les pages restent signalées À VÉRIFIER lorsque les contrôles d'orientation ou
de géométrie hérités du script original le demandent.

Les essais ont écrit leurs images et coordonnées dans `tmp/qwen35-check` ; les
empreintes des bruts et pages échantillonnés sont restées identiques. Aucun scan
physique n'a été déclenché. Les anciens modèles Qwen3-VL ont été supprimés ;
Qwen3.5 et le modèle d'embeddings préexistant sont conservés.

```powershell
uv run tools/scan_book_qwen.py correct "Les veilleurs" --page 23
uv run tests/test_scan_book_qwen.py
```

Les tests automatisés ne téléchargent aucun modèle et n'exigent pas de GPU.
Ils couvrent le protocole Ollama simulé, les coordonnées, les échecs, les sessions
partagées dans les deux sens, le verrou, l'export et l'interface avec WIA simulé.

## Fichiers et export

```text
scans/les-veilleurs/
    session.json
    raw/0001.png
    raw/0002.png
    pages/0001.png
    pages/0002.png
    les-veilleurs.pdf
```

`raw` conserve les acquisitions, `pages` les images corrigées. **R** remplace le
brut et l'image de la dernière page ; **C** modifie seulement son image corrigée.
Le manifeste fixe l'ordre et enregistre les résolutions effectives de chaque page,
ses dimensions, les réglages WIA et les transformations appliquées.

```powershell
uv run tools/scan_book.py export les-veilleurs
```

L'export ne charge ni WIA ni l'aperçu. Il assemble les images sans perte et crée
des pages aux dimensions physiques calculées depuis leurs pixels et ppp. Il
n'ajoute pas de couche de texte. Un export volumineux peut prendre du temps.
Une image absente ou illisible bloque l'export des variantes classique et DeepLab.
Qwen signale et ignore les pages dont une image est absente ; une image illisible
reste bloquante. Si aucune page ne reste à exporter, le PDF existant reste intact.
Après une correction ultérieure, relancer l'export pour actualiser le PDF.

Un verrou empêche deux commandes de modifier le même livre simultanément. Le
fichier `.session.lock` reste normalement présent après fermeture ; c'est le
verrou Windows, libéré à la fin du processus, qui protège la session.

Les images sont écrites avant le manifeste. En cas d'interruption, `.pending`
contient le brut ou le journal à récupérer automatiquement au prochain lancement.
Ne pas le supprimer. Une erreur d'écriture interceptée restaure l'ancienne page
et peut conserver la tentative dans `recovery/<identifiant>/`. Une rare erreur de
nettoyage peut laisser un dossier `.committed-*`, sans effet sur les pages validées.

## UTF-8 et diagnostic Windows

Le script force **UTF-8 sur stdin/stdout/stderr**, y compris en redirection, et
transmet `PYTHONUTF8=1` et `PYTHONIOENCODING=utf-8` à ses sous-processus. Les JSON
sont lus et écrits explicitement en UTF-8. Les touches de console utilisent
l'API Unicode de Windows (`getwch`) ; aucun `chcp` n'est nécessaire au script.
Pour lire ses fichiers avec Windows PowerShell 5, préciser `Get-Content -Encoding UTF8`.

Si `devices` ne trouve rien : allumer le scanner, vérifier l'USB, fermer les autres
logiciels de numérisation, vérifier le pilote Epson WIA, puis contrôler le service :

```powershell
Get-Service stisvc
```

La compatibilité des réglages dépend du pilote. Les essais physiques du V330
sont reportés après le redémarrage demandé ; aucun test de scan réel n'a été réalisé.

Si Tkinter signale `Can't find a usable init.tcl`, essayer depuis un terminal
Windows normal : un environnement d'exécution restreint peut bloquer Tcl/Tk.
Si l'erreur persiste, utiliser un Python incluant Tcl/Tk, par exemple
`uv run --python 3.12 tools/scan_book.py scan les-veilleurs` avec une installation
3.12 fonctionnelle. `devices` et `export` n'ont pas besoin de Tkinter.

## Tests sans scanner

```powershell
uv run tests/test_scan_book.py
```

Tests de reprise, interruptions, retour arrière, exclusion concurrente, DPI et
ordre PDF, accents et redirections UTF-8, pages synthétiques inclinées ou illustrées,
réglages WIA simulés et interactions de l'aperçu avec un scanner simulé. Les tests
utilisent des dossiers temporaires et ne contactent aucun scanner.

Sur un environnement sans interface graphique : définir `SCAN_BOOK_SKIP_GUI=1`
pour ignorer explicitement les tests Tkinter. Sur Windows avec un bureau,
exécuter la suite complète.
