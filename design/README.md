# Maquette de référence

Les fichiers de `maquette/` viennent d'un outil de design (format `.dc.html`, avec son propre moteur). Ils servent de référence pour la mise en page, les couleurs, les textes et l'interaction de la fiche matière, pas de code à reprendre. Toutes leurs données sont fictives.

- `Main.dc.html` : écran Marchés et fiche matière (liste de suivi cliquable, graphique avec événements, analyse IA avec la pression de prix, événements, carte, cas comparables, goulots en préparation). Le bloc `data()` du script montre la forme des données attendues par l'écran.
  - Pression de prix : flèche, libellé (« Forte hausse » à « Forte baisse ») et une phrase d'explication, affichée même quand la matière n'a pas de cotation.
  - Carte « Cas comparables » : pour l'événement sélectionné dans la liste, un point par cas passé placé sur sa variation propre (variation à 20 séances hors mouvement général du marché), la fourchette des cas de 10 % à 90 %, la médiane et, au-dessus, la fourchette estimée par l'IA. Les cas écartés (chevauchement avec un autre événement, brouillon IA non vérifié) restent visibles mais n'entrent pas dans le calcul. Un avertissement apparaît si l'estimation de l'IA sort de la fourchette des cas ; en dessous de 3 cas retenus, la carte affiche « pas assez de cas comparables » au lieu du graphique. Le texte dit toujours « observée après », jamais « causée par ».
- `Chaine.dc.html` : graphe de chaîne de l'acrylonitrile et panneau du maillon sélectionné ; bascule vers « Ma chaîne ».
- `MaChaine.dc.html` : écran Chaîne en mode « Ma chaîne », interactif. Coffre ouvert : Ma chaîne en surimpression (liens bleus d'épaisseur proportionnelle à la part de volume, sites privés en pointillé bleu, maillons non utilisés estompés), panneau du site sélectionné, encarts « Ce que Ma chaîne change » et « Concentration cachée ». Le cadenas de la barre du haut ferme le coffre (vue sans aucun indice sur les fournisseurs) ou l'ouvre (feuille Touch ID simulée) ; « Chaîne type » montre le verdict marché et l'adoption d'un maillon en trois champs ; « Mode présentation » floute noms et chiffres.
- `Ajout.dc.html` : ajout d'une matière par CAS, identification, progression de la reconstruction, couverture facultative.
- `Fiabilite.dc.html` : écran Fiabilité. Bascule « Marché / Ma matière » (dessinée, non câblée), indicateurs, prix réel avec les conseils datés (losange à l'émission, fourchette en éventail jusqu'à l'échéance, pastille juste ou manquée, anneau pour une hausse non signalée, ruban des verdicts sous la courbe), dossier du conseil sélectionné, registre cliquable synchronisé avec le graphique, diagramme de calibration et score de Brier. Le script montre les règles de résultat appliquées aux séries fictives.
- `Conformite.dc.html` : écran Conformité. Choix du modèle (article 24 ou questionnaire client), des matières et des mesures, aperçu A4 du rapport avec indice de concentration et repère de 65 %, contrôle de complétude, mention « brouillon » tant que le rapport est incomplet.

La carte de la maquette est une image statique ; l'app utilise MapLibre GL JS avec les tuiles OpenFreeMap.

## Jetons de design

| Rôle | Valeur |
| --- | --- |
| Fond de page | `#0B0E13` |
| Panneaux | `#121720` ; ligne ou survol sélectionné `#1A212C` ; barre du haut `#0F131A` |
| Bordures | `#263040` (panneaux), `#2E3846` (maillons) |
| Texte | `#E8EBF0` ; secondaire `#A3ADBB` (contraste 7,9:1 sur les panneaux) |
| Interaction et sélection | `#6CB2FF` |
| Hausse / baisse | `#3CCB91` / `#FF7B7B`, toujours avec le signe |
| Verdict « Anticiper stock », statut « En cours » | `#FF8A4C` (fond plein et texte `#14171C` pour le verdict) |
| Verdict « Problème à X sem. », statut « En préparation » | `#F5B841` (contour) |
| « Rien à signaler », « Signal précoce » | `#B8C2CF` sur bordure `#3A4656` |
| Sites de la chaîne sur la carte | `#8A95A5` |
| Résultat juste, confirmé | `#3CCB91` (pastille pleine ou contour, toujours avec le libellé) |
| Fourchette manquée, fausse alerte, hausse manquée | `#FF7B7B` (pastille pleine, contour ou anneau, toujours avec le libellé) |
| En cours | `#A3ADBB` sur bordure `#3A4656` |
| Aperçu imprimable (rapport) | papier `#FBFAF7`, texte `#16191F`, filets `#C9CED6`, alerte `#B4321F` |
| Ma chaîne (surimpression) | liens `#6CB2FF`, épaisseur de 3,5 à 6 px selon la part de volume ; site privé : bordure `#6CB2FF` en pointillé ; maillon non utilisé : voile `rgba(11, 14, 19, 0.70)` et mention « pas chez toi » |
| Coffre | ouvert : pastille `#6CB2FF` sur `rgba(108, 178, 255, 0.10)` avec le temps restant ; fermé : `#B8C2CF` sur bordure `#3A4656` ; donnée privée remplacée par « motif dans le coffre » |
| Mode présentation | flou de 6 px sur les noms de fournisseurs, parts, stocks et prix ; bouton et bandeau `#F5B841` |
| Pression de prix | forte hausse `#FF8A4C`, hausse `#F5B841` ; neutre, baisse et forte baisse `#B8C2CF` (une baisse n'appelle pas d'action, d'où la couleur neutre) ; toujours avec la flèche et le libellé |
| Cas comparables | cas retenu : point plein `#6CB2FF` ; exclu pour chevauchement : contour pointillé `#A3ADBB` ; brouillon IA : contour `#F5B841` ; fourchette 10 % à 90 % : `#6CB2FF` à 10 % d'opacité ; médiane : trait `#6CB2FF` ; estimation de l'IA : crochet `#FF8A4C` ; zéro : pointillé `#3A4656` |
| Polices | IBM Plex Sans (texte), IBM Plex Mono (prix, CAS, chiffres), auto-hébergées |
| Rayons | 10 px (panneaux), 8 px (lignes, boutons), 999 px (pastilles) |
| Cibles cliquables | 44 px de haut au minimum |

Tous les textes respectent un contraste d'au moins 4,5:1. La couleur n'est jamais le seul repère : chaque statut a son libellé.
