# Spécification fonctionnelle de Goulot

## 1. Parcours

1. L'utilisateur saisit un nom ou un numéro CAS.
2. L'app identifie la substance, affiche un devis, puis reconstruit la chaîne en quelques minutes après confirmation.
3. L'utilisateur relit la chaîne et corrige les maillons douteux, puis la valide. C'est la « chaîne type » : une moyenne du marché, tirée de sources publiques.
4. S'il le souhaite, il déclare sa chaîne réelle, « Ma chaîne » : ses fournisseurs, leurs sites, sa part de volume, ses délais et ses stocks. Elle reste chiffrée sur le Mac, derrière le coffre, et n'est jamais envoyée à l'IA (section 11).
5. La validation génère les règles de veille et télécharge l'historique des prix des séries liées (section 6) ; l'utilisateur peut lancer, avec devis, la préparation des cas comparables. Les sources sont relues toutes les 3 heures.
6. L'IA n'est appelée que si un événement touche un maillon qui pèse, pour le marché ou pour l'utilisateur. Elle juge l'événement à l'échelle du marché ; le code calcule sur le Mac ce qu'il change pour l'utilisateur, le verdict et le délai X ; une alerte part si le verdict change.

Le Mac peut dormir : toutes les 10 minutes, un contrôle lance un cycle si le dernier cycle terminé a commencé il y a plus de 2 h 45 (heure murale). Au réveil, un cycle part donc dans les 10 minutes et rattrape les collectes manquées (GDELT et la plupart des flux gardent un historique), puis les alertes partent. La sauvegarde quotidienne suit le même principe. « Rien à signaler » ne s'affiche que si la veille a vraiment tourné récemment (section 5).

## 2. Modèle de données (SQLite)

| Table | Contenu principal |
| --- | --- |
| `materials` | id, nom, CAS, formule, codes SH candidats, statut (brouillon, validée, archivée), dates |
| `chain_nodes` | id, matière, clé, type, nom, produit, pays ISO, lieu, lat, lon, niveau, part mondiale, stock tampon (jours), capacité, confiance, modifié par l'utilisateur, `valid_from`, `recorded_at` |
| `chain_edges` | matière, origine, destination, relation, mode de transport, délai (jours), part, confiance, modifié par l'utilisateur |
| `chain_risks`, `chain_dates` | risques structurels et échéances connues issus de la reconstruction, avec leurs maillons et sources |
| `sources`, `node_sources`, `edge_sources` | URL, titre, éditeur, date ; liens vers les maillons |
| `watch_rules` | matière, type (mot-clé, zone, pays, code SH, détroit), valeur, rayon en km, langue ; chaîne type seulement : les règles issues de Ma chaîne ne sont jamais stockées (section 3) |
| `articles` | source, URL et empreinte, titre, langue, dates de publication, de première observation et de collecte, extrait, clé de lignée, règles déclenchées |
| `triage` | article, modèle, résultat JSON, coût |
| `events` | type, statut, titre, lieu, lat, lon, pays, première et dernière observation, `c_fait`, nombre de lignées, source officielle (oui ou non) |
| `event_articles`, `event_links` | articles d'un événement ; maillons touchés et part de l'offre exposée |
| `digests` | synthèses hebdomadaires : matière, semaine, sortie JSON, coût, date d'envoi |
| `analyses`, `hypotheses` | sortie d'analyse (versions du prompt et du schéma, coût) ; hypothèses de prix et leur résultat observé |
| `verdicts` | matière, portée (`market` ou `personal`), verdict, pression de prix (de −2 à +2), X en semaines (valeur centrale, basse et haute), fenêtre d'action, explication, entrées du calcul (chiffrées), date du dernier cycle de collecte réussi, date. Pour un verdict personnel, X, fenêtre d'action et explication sont chiffrés |
| `collect_runs` | cycles de collecte : début, fin, sources qui ont répondu et sources en échec, par famille (sert à la fraîcheur de la veille) |
| `instruments`, `prices` | séries de prix publiques avec tout leur historique disponible : cotations, prix officiels chinois, prix douaniers unitaires, indices, indices proxy ; source, unité, devise et rang de chaque série (section 6) |
| `instrument_links` | lien entre une matière et ses séries : rôle (cotation directe, prix officiel, prix douanier d'un code SH, indice, intrant, dérivé), poids, validé ou non par l'utilisateur |
| `historical_events` | bibliothèque de cas passés (section 6) : date de la première annonce publique, type, lieu, pays, type de maillon, matières touchées, part de la capacité mondiale touchée et sa base, sens de l'effet, durée, résumé, sources, état (brouillon IA ou vérifié par l'utilisateur) |
| `event_responses` | réaction de chaque série liée à chaque cas, calculée par le code : variation brute et variation propre à 1, 5, 20 et 60 séances, facteur de marché et β utilisés, chevauchement avec un autre cas, version des données |
| `domain_rules` | domaines ajoutés ou reclassés par l'utilisateur (liste blanche, fiabilité), en couche au-dessus de `config/` ; jamais dans le dépôt |
| `user_coverage`, `user_quotes`, `user_purchases` | stock en semaines, délai fournisseur, seuil de hausse ; devis ; achats déclarés (date, quantité, prix) pour le gain estimé et comme série de prix privée. Chiffrés, jamais envoyés à l'IA |
| `private_chain` | « Ma chaîne » par matière : révisions en ajout seul (numéro, date, contenu chiffré, version de clé). Une modification crée une révision ; chaque verdict personnel note la révision utilisée |
| `user_signals` | signaux fournisseurs déclarés : date, type, valeur, date d'effet, site de Ma chaîne, commentaire. Chiffrés |
| `private_links` | liens entre un événement et un site de Ma chaîne, calculés sur le Mac (section 11) : une ligne chiffrée par Ma chaîne, réécrite à chaque cycle. Chiffrés |
| `vault_keys`, `vault_passkeys`, `vault_tokens` | clés de données enveloppées et copie de la clé maîtresse enveloppée par la clé de secours ; clés d'accès (passkeys) enregistrées : identifiant, clé publique, compteur ; jetons de coffre, stockés sous forme d'empreinte (section 11) |
| `ledger` | registre en ajout seul : numéro, horodatage, matière, type, contenu JSON, champs privés chiffrés, empreinte précédente, empreinte (section 7) |
| `compliance_exports`, `mitigations` | rapports de conformité (modèle et version, matières, base de la cartographie, empreinte, entrée du registre ; jamais le contenu d'un rapport fondé sur Ma chaîne) ; mesures d'atténuation retenues par matière (chiffrées) |
| `alerts`, `llm_calls`, `settings`, `audit_log` | alertes envoyées ; appels IA et coûts ; réglages non secrets ; journal des événements de sécurité |

Le référentiel est bitemporel : chaque maillon garde sa période de validité et sa date d'enregistrement, pour qu'un rejeu ne voie que ce qui était connu à la date rejouée.

Ce qui est chiffré, et comment : section 11. Ne sont pas chiffrés les noms des matières, la chaîne type, les articles, les événements et les prix : tout cela vient de sources publiques, et le nom des matières part de toute façon dans les requêtes de veille.

## 3. Reconstruction de la chaîne

1. **Identifier.** PubChem (PUG REST) pour CAS, nom, formule et synonymes ; Wikidata (propriété P231, numéro CAS) en complément. Code douanier : ECICS (base de la Commission européenne, consultation web sans API connue ; si l'interrogation automatique est impossible ou interdite, prendre les codes SH proposés par l'IA et les marquer « à vérifier »).
2. **Rechercher.** Appel 1 : `prompts/chain_research_v1.md`, outil de recherche web, rapport sourcé. Le code remplace chaque citation par un repère `[srcN]` et garde URL, titre, éditeur et date dans `sources`.
3. **Structurer.** Appel 2 : `prompts/chain_structure_v2.md` et `schemas/supply_chain_core_v1.json` (matière, maillons, liens) ; appel 3, facultatif : `prompts/chain_context_v1.md` et `schemas/supply_chain_context_v1.json` (voies de production, substituts, risques, échéances). Un schéma unique est refusé par l'API, trop gros à compiler (16 octobre 2026). Le graphe va de l'amont vers la matière achetée, seul maillon sans successeur. Le code vérifie la sortie avec les contrôles de `schemas/check_schemas.py` : identifiants uniques, liens valides, chemin de chaque maillon jusqu'à la matière, absence de cycle, parts entre 0 et 100, sources présentes dans la liste fournie, CAS inchangé. Le pays déduit des coordonnées (frontières Natural Earth embarquées) doit correspondre à `country_iso2`, sinon lat et lon passent à null. Une sortie non conforme au schéma est rejetée et l'appel relancé une fois ; les autres anomalies passent le maillon en confiance faible et s'affichent sur l'écran « Chaîne ».
4. **Recouper.** Confronter les parts par pays aux bases publiques disponibles (USGS Mineral Commodity Summaries pour les minerais, UN Comtrade pour les échanges). Un écart de plus de 30 % relatifs passe le maillon en confiance faible avec un commentaire.
5. **Compléter les délais.** Trajets maritimes estimés quand deux ports localisés sont connus : distance orthodromique × 1,3, à 13 nœuds (décision du 16 octobre 2026, à la place de `searoute`). Sinon, valeurs par défaut :

| Élément | Valeur par défaut |
| --- | --- |
| Stock tampon d'une mine | 14 jours |
| Stock tampon d'une usine de transformation | 21 jours |
| Stock tampon d'un port | 7 jours |
| Stock tampon d'un distributeur | 28 jours |
| Camion dans une même région | 3 jours |
| Barge fluviale | 5 jours |
| Stock tampon d'un pays producteur (ensemble d'usines) | 21 jours |
| Stock tampon d'une matière, d'une entreprise, d'un détroit ou d'une voie fluviale | 0 jour |
| Fret maritime sans ports connus | 30 jours |
| Autre mode de transport ou mode inconnu | 7 jours |
| Lien sans transport (intrant, production, coproduit) | 0 jour |

Chaque valeur par défaut est réglable et affichée comme hypothèse.

6. **Valider.** Écran « Chaîne » : graphe de l'amont vers la matière, couleur par statut, bordure pointillée pour la confiance faible, panneau du maillon sélectionné (rôle, événement, délai jusqu'à l'utilisateur, alternatives, sources, confiance), boutons « Corriger » et « Voir les sources ». Rien n'est surveillé avant validation.
7. **Armer la veille.** Règles générées : noms des sites, entreprises, ports et détroits (en anglais, dans la langue locale et en français), zone de 50 km autour de chaque site géolocalisé, pays producteurs combinés au nom de la matière ou de ses intrants, codes SH pour la politique commerciale. Pour chaque site de Ma chaîne, l'utilisateur choisit le niveau de surveillance en ligne : « aléas seulement » (zone de 50 km sur les flux d'aléas, filtrés sur le Mac : rien ne part), « ville et matière » (par défaut : le nom de la ville et celui de la matière entrent dans les requêtes d'actualités) ou « ville, matière et entreprise ». Le nom d'une entreprise n'entre donc dans une requête que sur autorisation, et jamais le fait qu'elle fournit l'utilisateur. Ces règles ne sont jamais stockées en clair : la veille les recalcule en mémoire à chaque cycle à partir de Ma chaîne, et un article qu'elles ont déclenché porte seulement la mention « règle privée ».

## 4. Veille

### Sources gratuites (détail et points d'accès dans `config/sources.example.yaml`)

| Famille | Sources | Fréquence |
| --- | --- | --- |
| Actualités | GDELT DOC 2.0, Google News RSS par requête, flux RSS des producteurs | 3 h |
| Aléas | GDACS, USGS (séismes), NASA FIRMS (incendies, clé gratuite), NOAA NHC (cyclones) | 3 h |
| Logistique | IMF PortWatch (passages quotidiens dans 28 détroits), PEGELONLINE (niveau du Rhin à Kaub) | quotidienne |
| Politique commerciale | Federal Register (États-Unis), annonces chinoises via la recherche d'actualités, sanctions OFAC et UE | quotidienne |
| Énergie | EIA (clé gratuite) | quotidienne |
| Prix | voir section 6 | quotidienne ou mensuelle |

### Traitement

1. **Collecte** avec le client HTTP maison, cache et respect des limites de débit.
2. **Filtre par règles (gratuit)** : un article passe s'il déclenche au moins une règle de veille ; un aléa passe s'il tombe dans la zone d'un site.
3. **Tri** par Haiku 5.5, sans réflexion (`prompts/triage_v1.md`, `schemas/triage_v1.json`), en lot. Le tri reçoit les maillons de la chaîne type, jamais ceux de Ma chaîne. Les articles non pertinents sont gardés 30 jours pour audit puis supprimés.
4. **Regroupement** en événements : même type, même maillon ou même lieu, fenêtre de 7 jours, titres proches. Les liens entre un événement et un site de Ma chaîne sont calculés ensuite sur le Mac, sans IA, à partir des entités et du lieu extraits au tri et de la distance aux sites (section 11), puis stockés chiffrés dans `private_links`.
5. **Corroboration** (`config/reliability.yaml`). Fiabilité par domaine : officielle 0,9 ; agence, presse financière ou spécialisée 0,7 ; presse générale 0,5 ; tout domaine inconnu ou réseau social 0,3. Une lignée regroupe une source d'origine et ses reprises : même URL canonique, texte quasi identique, même source d'origine citée (`cited_origin` du tri) ou même groupe de presse ; une lignée prend la fiabilité de sa meilleure source. Un site de reprise (Yahoo Finance, MSN, Google News…) prend la fiabilité du fournisseur indiqué par la donnée structurée de la plateforme, jamais par la signature ni par `cited_origin`, qui servent seulement à regrouper. Un communiqué de presse payant vaut 0,3 où qu'il soit publié. Réalité du fait : `c_fait = 1 − Π(1 − r_i)` sur les lignées. Événement confirmé si `c_fait ≥ 0,75` et si au moins une lignée a une fiabilité ≥ 0,7 ; sinon « signal précoce ». Quatre faux sites inconnus ne confirment donc rien.
6. **Déclenchement de l'analyse** si l'événement est confirmé (ou signal précoce avec sévérité préliminaire ≥ 3) et touche un maillon qui pèse au moins 0,5 % de l'offre mondiale de son niveau, ou un maillon critique (détroit, fournisseur quasi unique), ou un site qui pèse pour l'utilisateur d'après Ma chaîne (au moins 10 % de son volume, réglable, ou son fournisseur unique), ou s'il coïncide avec une anomalie de prix (|z| > 3 sur le rendement journalier). Le troisième cas est décidé sur le Mac et ne laisse aucune trace dans le dossier envoyé.
7. **Analyse** par Sonnet 5.5 (`prompts/impact_v1.md`, `schemas/impact_assessment_v1.json`), en trois appels partiels sur le même dossier (compréhension et conclusion ; impacts et signaux à suivre ; hypothèses de prix), réassemblés et contrôlés par le code ; une partie manquante fait échouer l'analyse. Le dossier envoyé contient l'événement, les extraits des sources (identifiants S…), les maillons concernés de la chaîne type et leurs données comme `<reference_data>` (identifiants R…), la matière et ses intrants directs comme `tracked_commodities`, le contexte de prix, les taux de base, les seuils, et les cas comparables vérifiés comme `<analogues>` (identifiants A…, section 6). Il ne contient jamais les données d'achat de l'utilisateur, ni rien de Ma chaîne ou de ses signaux fournisseurs : un site privé n'y apparaît que tel que les articles le décrivent, sans indication qu'il compte pour l'utilisateur. L'IA évalue l'événement à l'échelle du marché.
8. **Goulots en préparation** : les dates futures extraites au tri et les échéances connues de la chaîne (`upcoming_dates`) alimentent le bandeau « Goulots en préparation » et le calendrier.

## 5. Verdict et délai X

Entrées, pour chaque événement actif lié à la matière dont l'analyse n'est pas en quarantaine :

- `s` : la plus haute `severity` des `commodity_impacts` dont `net_balance_effect` vaut `tightening`, sur la matière ou l'un de ses intrants directs. Un événement sans impact resserrant n'entre pas dans le verdict.
- `p` et hausse attendue : seulement depuis les hypothèses `metric: outright_price`, `direction: up`, horizon `20d`, sur un instrument de la matière ou d'un intrant direct. `p` est la plus haute `probability` ; la hausse attendue est le milieu de `[move_p10, move_p90]` de cette hypothèse, en pourcentage (converti avec le dernier prix si `move_unit` vaut `price_units`). Sans hypothèse de ce type, la règle de prix ne s'applique pas : aucune probabilité par défaut.
- Durée haute : `expected_duration_days.high`, sinon `low`, sinon 30 jours (hypothèse affichée).
- `s_perso` (verdict personnel) : `s` mesure le marché, en part de l'offre mondiale ; un fournisseur petit mais essentiel y vaut 0 ou 1. Pour un événement perturbant qui touche un site de ses chemins (confirmé, ou signal fournisseur ; arrêt industriel, catastrophe, météo extrême, grève, défaillance d'infrastructure, conflit, troubles, cyberattaque, restriction d'exportation ou sanction visant le site ; annoncé, en cours ou en escalade), `s_perso = max(s, 2)` si l'exposition `e` (ci-dessous) atteint 10 %, `max(s, 3)` si elle atteint 50 %, et `s` sinon ; `s` vaut 0 sans analyse ni impact resserrant.

Deux portées :

- **Verdict marché** : données publiques seules. Il ne retient que les événements reliés à la chaîne type et déclenchés par un critère public (section 4), avec les seuils du verdict marché (réglages non privés, hausse de 5 % par défaut) et le délai fournisseur par défaut. C'est lui que reçoit la synthèse rédigée par l'IA, et lui seul alimente la colonne « Marché » de l'écran Fiabilité.
- **Verdict personnel** : calculé quand Ma chaîne, des signaux fournisseurs ou une couverture existent pour la matière. Mêmes règles, avec les chemins, parts, stocks, délais et seuils de l'utilisateur, `s_perso` à la place de `s`, et ses signaux comme événements. L'écran et les alertes montrent le verdict personnel s'il existe, le verdict marché sinon.

Le code calcule :

- **X**, délai avant impact chez le fournisseur, en semaines : somme, sur le chemin le plus court du maillon touché jusqu'à la matière, des délais de transport et de transformation et des stocks tampons des maillons intermédiaires, divisée par 7. La matière achetée est le maillon final du graphe ; le calcul utilise `networkx`. Pour le verdict personnel, le chemin est celui de Ma chaîne (du maillon touché au fournisseur de l'utilisateur, puis jusqu'à lui) et les valeurs saisies remplacent les valeurs par défaut. Si la durée haute de l'événement est inférieure à la somme des stocks tampons, l'effet physique est absorbé : seul l'effet prix reste.
- **Fourchette de X** : chaque stock tampon par défaut du chemin compte pour la moitié de sa valeur (X bas) et pour une fois et demie (X haut) ; les valeurs saisies par l'utilisateur comptent telles quelles. L'écran affiche « 3 à 5 sem. » ; les règles utilisent la valeur centrale. Sans valeur par défaut sur le chemin, la fourchette se réduit à un point.
- **W**, fenêtre d'action : X moins le délai fournisseur. Verdict personnel : délai du lien concerné dans Ma chaîne, sinon délai saisi dans la couverture. Verdict marché, ou rien de saisi : 4 semaines, signalé comme hypothèse.
- **e**, exposition (verdict personnel) : part du volume de l'utilisateur qui passe par le maillon touché, d'après Ma chaîne (calcul en amont de ses sites : section 11) ; 100 % sans Ma chaîne.

| Verdict | Règle par défaut (seuils réglables) |
| --- | --- |
| Anticiper stock | règle physique : un événement confirmé avec `s ≥ 2` et `W ≤ 2` semaines (verdict personnel : `s_perso ≥ 2` et `e ≥ 10 %`) ; ou règle de prix : un événement confirmé avec `p ≥ 0,5` et une hausse attendue au moins égale au seuil de la portée (5 % par défaut) |
| Problème potentiel à X semaines | sans remplir la règle précédente : un événement confirmé, ou signal précoce appuyé par au moins une lignée de fiabilité ≥ 0,7, avec `s ≥ 2` (verdict personnel : `s_perso ≥ 2` et `e > 0`), ou avec `p ≥ 0,25` et une hausse attendue au moins égale au seuil. X = le plus petit X des événements retenus ; 4 semaines (horizon de 20 jours de bourse) pour un événement retenu par la seule règle de prix |
| Rien à signaler | tous les autres cas |

En mode règles seules (plafond atteint), `s` est estimé par la part de l'offre exposée (barème de `prompts/impact_v1.md`), l'effet est supposé resserrant, seule la règle physique s'applique, et le verdict est marqué « estimation sans IA ».

**Pression de prix**, calculée pour chaque matière et chaque portée, avec ou sans cotation, et affichée à côté du verdict :

| Pression | Règle par défaut (seuils réglables) |
| --- | --- |
| Forte hausse (+2) | un événement confirmé resserrant de sévérité ≥ 3 (`s_perso` pour la portée personnelle), ou `p ≥ 0,5` sur une hausse au moins égale au seuil |
| Hausse (+1) | un événement confirmé, ou signal précoce appuyé par une lignée de fiabilité ≥ 0,7, resserrant de sévérité ≥ 2 ; ou `p ≥ 0,25` sur une hausse au moins égale au seuil |
| Neutre (0) | tous les autres cas |
| Baisse (−1) | rien qui justifie une hausse, et un événement confirmé desserrant (`net_balance_effect` vaut `loosening`) de sévérité ≥ 2, ou `p ≥ 0,25` sur une baisse au moins égale au seuil |
| Forte baisse (−2) | rien qui justifie une hausse, et un événement confirmé desserrant de sévérité ≥ 3, ou `p ≥ 0,5` sur une baisse au moins égale au seuil |

La pression ne suppose aucun prix de base. Un pourcentage ne l'accompagne que s'il vient d'une hypothèse de prix ou des cas comparables (section 6) ; sans série propre à la matière, il s'affiche comme « ordre de grandeur, par analogie », avec le nom de la série d'où il vient. Une pression à la hausse alors que la marge de la matière se resserre (section 6) porte la mention « hausse des coûts plutôt que pénurie ». Une baisse s'affiche « pas d'urgence à acheter » : ce n'est jamais un conseil de vente.

**Signaux fournisseurs** (section 11), pour le verdict personnel :

| Signal | Effet |
| --- | --- |
| Force majeure, rupture | événement privé confirmé sur le site déclaré, `s_perso` au moins 3 ; X = délai de ce site jusqu'à l'utilisateur (0 pour son fournisseur direct) |
| Allocation, délai allongé | événement privé confirmé sur le site déclaré, `s_perso` au moins 2 ; X comme ci-dessus ; un délai allongé d'au moins 2 semaines ou de 50 % (réglable) remplace le délai du lien pour le calcul de W ; en dessous, il est seulement noté |
| Hausse annoncée | si elle atteint le seuil : règle de prix remplie (probabilité 1) ; X = semaines jusqu'à la date d'effet, qui entre au calendrier : « Anticiper stock avant le 1er décembre » |
| Retour à la normale | clôt les signaux précédents du même site |

Un signal est la parole d'un fournisseur à son client : il compte comme une source de fiabilité 0,9, sans corroboration. Sans nouvelle confirmation, il expire après 90 jours, avec un rappel à l'utilisateur. Les événements privés (signaux, sites présents seulement dans Ma chaîne) n'entrent jamais dans le verdict marché.

**Fraîcheur.** Un cycle de collecte est réussi si chacune des familles Actualités et Aléas a au moins une source qui a répondu, et si le tri des articles candidats a abouti (ou s'il n'y en avait aucun) : une clé d'API morte ne doit pas ressembler à un calme plat. « Rien à signaler » ne s'affiche que si le dernier cycle réussi date de moins de 12 heures (réglable) ; sinon l'écran affiche « Non vérifié depuis … » en gris hachuré, et la synthèse le signale. Les autres verdicts restent affichés, avec l'heure de la dernière vérification.

Chaque verdict garde ses entrées (`inputs_json`) pour être expliqué et rejoué. Comme elles contiennent des données d'achat, elles sont chiffrées et ne sont jamais transmises à l'IA : la synthèse hebdomadaire ne reçoit que le verdict marché, X, le mode et, par événement retenu, identifiant, statut, `s` et `p` (ou « none »). Le code ajoute lui-même au message la ligne du verdict personnel (verdict, fenêtre d'action, « motif dans l'app »).

## 6. Prix

Périmètre : les matières chimiques identifiées par leur CAS, leurs dérivés, et les grands repères (Brent, WTI, gaz). Points d'accès et conditions : `config/sources.example.yaml`.

**Ordre de préférence des séries** : sources officielles et gratuites d'abord, toutes appelées par le client HTTP maison, sans `yfinance` ni `akshare`, qui font leurs propres appels réseau.

| Rang | Série | Sources | Rythme |
| --- | --- | --- | --- |
| 1 | cotation directe de la matière | bourses chinoises (Dalian, Zhengzhou, Shanghai et INE, Guangzhou) ; EIA pour le pétrole et le gaz américain ; Banque mondiale pour quelques produits en moyenne mensuelle (urée, DAP, chlorure de potassium, gaz européen) | quotidien, ou mensuel pour la Banque mondiale |
| 2 | prix de marché officiel | Bureau national des statistiques chinois (50 produits) | tous les 10 jours |
| 3 | prix douanier unitaire (valeur divisée par quantité) du code SH de la matière | douanes américaines (10 chiffres, par pays d'origine), Eurostat Comext (8 chiffres), UN Comtrade (6 chiffres) | mensuel, 1 à 2 mois de retard |
| 4 | indice de prix de sa famille | BLS, Eurostat | mensuel |
| 5 | indice proxy des intrants directs | séries de rang 1 à 3 des intrants, à parts égales par défaut, pondération modifiable dans Réglages | selon les intrants |
| dépannage | points d'accès non officiels (Yahoo, Stooq), usage personnel | seulement pour une série absente des rangs 1 à 5, comme le gaz européen au jour le jour | quotidien |

- `instrument_links` relie chaque matière à ses séries. La reconstruction propose les liens (codes SH, intrants, dérivés) ; l'utilisateur les valide. Un prix douanier n'est pris comme série de la matière que si son code SH lui est propre (pas une rubrique « autres ») et si le volume du mois atteint un minimum réglable ; sinon il reste un indice faible.
- L'écran affiche toujours le rang de la série montrée : « cotation », « prix officiel », « prix douanier », « indice ».
- **Marge** : pour une matière cotée dont les intrants le sont aussi, l'écart entre son prix et le coût de ses intrants, calculé avec les rendements de la chaîne type. Une marge qui s'élargit signale une tension propre à la matière ; une hausse générale sans élargissement, une poussée des coûts.
- **Historique** : à l'ajout d'une matière, l'app télécharge une fois tout l'historique disponible de ses séries (EIA depuis 1987, Banque mondiale depuis 1960, chaque contrat chinois depuis son lancement, douanes sur plus de dix ans), puis le complète à chaque cycle.
- Les devis de l'utilisateur s'affichent à côté, coffre ouvert, mais n'entrent jamais dans les prix transmis à l'IA.
- Licences : usage personnel. Avant toute vente, vérifier les droits de redistribution, notamment pour le Brent de l'EIA, qui vient d'un fournisseur privé, et pour les bourses chinoises. BLS, Banque mondiale, Eurostat et douanes américaines sont réutilisables avec citation.
- L'app signale une source muette au-delà de son rythme normal : 24 heures pour une série quotidienne, 15 jours pour les prix officiels chinois, 75 jours pour une série mensuelle.
- Contexte transmis à l'analyse : variation depuis la première source, variation sur 5 jours, seuil de matérialité par horizon (un écart type des rendements de cet horizon sur un an glissant), taux de base (part historique des fenêtres où le mouvement dépasse ce seuil, à la hausse et à la baisse), et cas comparables.

### Cas comparables

But : remplacer l'ampleur devinée par l'IA par ce qui s'est réellement passé lors d'événements semblables, avec des chiffres que chacun peut vérifier.

1. **Bibliothèque** (`historical_events`). Chaque cas porte la date de la première annonce publique, le type (mêmes types que le tri), le lieu, le type de maillon, les matières touchées, la part de la capacité mondiale touchée et sa base, le sens de l'effet, la durée et ses sources. L'IA prépare les cas par recherche web pour chaque matière suivie (`prompts/case_research_v1.md`, puis `prompts/case_structure_v1.md` et `schemas/historical_case_v1.json`, en deux appels comme la reconstruction), avec devis et confirmation, sur le plafond des reconstructions. Le code joint à la recherche, dans `<leads>`, les entrées des bases publiques d'événements passés (famille `history` de `config/sources.example.yaml` : eMARS, archives GDACS, ReliefWeb) qui touchent un pays ou un site de la chaîne type, jamais de Ma chaîne ; une piste citée entre dans `<sources>` avec son adresse. Le code vérifie qu'au moins une source citée a une fiabilité ≥ 0,7 (`config/reliability.yaml`) et garde son adresse. Un cas reste « brouillon IA » tant que l'utilisateur ne l'a pas vérifié, en un clic, sources sous les yeux ; un brouillon ne compte ni dans les médianes ni dans le dossier de l'analyse. Objectif de départ : 30 à 50 cas sur les matières suivies et leurs intrants.
2. **Réaction calculée par le code, jamais par l'IA** (`event_responses`). Pour chaque série liée : variation à 1, 5, 20 et 60 séances (1, 2 et 3 mois pour une série mensuelle) depuis le dernier prix connu avant l'annonce. Variation propre = variation brute − β × variation du facteur de marché : Brent pour la pétrochimie, gaz européen pour les engrais azotés, aucun pour le lithium et le silicium (mentionné) ; β estimé sur les 250 séances précédentes, 120 au moins. Un autre cas touchant la même série à 10 jours près marque les deux comme « chevauchement » : ils sortent des médianes par défaut.
3. **Ressemblance**. Points : même type d'événement (3), même type de maillon (2), part de capacité dans la même tranche (2), même région (1), même saison (1), même état de marché, c'est-à-dire marge en hausse ou en baisse sur 20 séances (1). Les cinq cas les plus proches au-dessus du seuil sont retenus.
4. **Affichage**. « 4 cas comparables (2017, 2021, 2022, 2025) : variation propre médiane de +X % à 20 séances, entre a et b % » ; chaque cas s'ouvre sur ses sources et son graphique. Moins de 3 cas : « pas assez de cas comparables ». Toujours « hausse observée après », jamais « causée par ».
5. **Usage par l'analyse**. Le dossier reçoit, pour chaque cas retenu, son identifiant A…, son titre, sa date, son type, sa part de capacité et sa variation propre à 1, 5 et 20 séances sur un instrument du dossier ; `prompts/impact_v1.md` demande déjà de partir de ces réactions mesurées, puis d'ajuster. Un cas de la bibliothèque ne prouve jamais la fiabilité de Goulot, puisque le modèle connaît déjà son issue (section 7).
6. **Rejeu**. À une date passée, seuls les cas annoncés avant cette date et les prix connus à cette date sont utilisés.

## 7. Registre et fiabilité

Chaque conseil est inscrit au registre le jour où il est émis, puis comparé à ce qui s'est passé. Le registre est la seule source des statistiques de fiabilité : rien n'est recalculé à partir d'un état modifié après coup.

**Registre (`ledger`, `goulot/ledger/`)**

- Ajout seul : aucune entrée n'est modifiée ni supprimée, migrations comprises. Une correction est une nouvelle entrée qui renvoie à l'ancienne.
- Types d'entrée : `verdict` (chaque changement de verdict, marché ou personnel, et le verdict de chaque matière au moment de la synthèse hebdomadaire) ; `hypothesis` (chaque hypothèse de prix d'une analyse) ; `outcome` (résultat calculé à l'échéance) ; `user_feedback` (« tension constatée » ou « rien constaté ») ; `signal` (signal fournisseur déclaré) ; `pressure` (chaque changement de pression de prix, marché ou personnelle) ; `missed_move` (hausse non signalée) ; `settings_change` (changement d'un seuil du verdict marché ou des règles de résultat ; un seuil personnel va dans les champs privés) ; `export` (rapport de conformité) ; `correction`.
- Contenu : numéro d'ordre, horodatage UTC, matière, type, contenu en JSON canonique (verdict et portée, X et sa fourchette, fourchette p10–p90, probabilité, horizon, versions du modèle, du prompt et du schéma, empreinte à clé des entrées du calcul), empreinte de l'entrée précédente, empreinte SHA-256 de l'entrée.
- L'empreinte des entrées du calcul est un HMAC-SHA-256 avec une clé dédiée du coffre : une empreinte simple permettrait de retrouver des valeurs privées peu nombreuses (stock en semaines, délai, seuil) en essayant toutes les combinaisons.
- L'empreinte de l'entrée couvre l'empreinte précédente, le contenu public et l'empreinte du chiffré des champs privés : la chaîne se vérifie sans la clé, et un export public garde l'empreinte des champs privés sans leur contenu.
- Champs privés à part et chiffrés (fenêtre d'action, X d'un verdict personnel, stock, quantités, prix payés, seuils personnels, exposition, motifs tirés de Ma chaîne et des signaux) : jamais dans un export public de l'historique, jamais envoyés à l'IA. Ils ne sont jamais rechiffrés : une nouvelle clé de données ne sert qu'aux nouvelles entrées.
- `goulot ledger verify` et `goulot doctor` contrôlent la chaîne d'empreintes. Une rupture s'affiche en rouge sur l'écran Fiabilité et dans « Bilan sécurité ».
- Ancrage : l'empreinte de la dernière entrée figure dans chaque synthèse hebdomadaire, ce qui en garde une copie datée hors du Mac. Après le MVP : horodatage RFC 3161 ou OpenTimestamps.

**Règles de résultat** (seuils réglables)

| Prédiction | Réussie si | Sinon |
| --- | --- | --- |
| Hypothèse de prix (horizon 1, 5 ou 20 jours de bourse) | la variation constatée à l'échéance tombe dans [p10, p90] : « fourchette juste ». Le sens est juste si elle dépasse le seuil de matérialité dans le sens annoncé | fourchette manquée |
| « Anticiper stock » ou « Problème potentiel à X semaines » | dans la fenêtre [émission ; émission + max(X, 4 semaines) + 2 semaines], la matière ou son indice monte d'au moins le seuil de la portée du conseil, ou, pour un verdict personnel, l'utilisateur déclare une tension chez son fournisseur (délai, allocation, hausse de prix) | fausse alerte |
| « Rien à signaler » | aucune hausse au moins égale au seuil dans les 8 semaines | hausse manquée |
| Pression de prix différente de 0 | dans les 20 séances (2 mois pour une série mensuelle, 3 mois pour un prix douanier), la série de meilleur rang liée à la matière dépasse son seuil de matérialité dans le sens annoncé ; pour la portée personnelle, la série privée compte aussi. Une série de rang 4 ou 5 donne un résultat « résolu sur indice » | sens manqué si elle le dépasse dans l'autre sens ; « sans effet » si elle ne le dépasse dans aucun sens ; « non résolu » sans série observée |

- Toute hausse au moins égale au seuil sur 20 jours de bourse, sans alerte dans les 8 semaines précédentes, crée une entrée `missed_move`.
- Avance : nombre de semaines entre le conseil et le jour où la hausse franchit le seuil.
- Sans série de prix (beaucoup de spécialités), la vérité terrain d'un verdict personnel vient de l'utilisateur : à l'échéance, l'app lui demande en un clic s'il a constaté une tension.
- Série privée : ses devis et achats déclarés (`user_quotes`, `user_purchases`) et ses signaux fournisseurs. Une hausse au moins égale au seuil entre le dernier devis ou achat avant la fenêtre et un devis ou achat dans la fenêtre vaut hausse constatée ; un signal « hausse annoncée », « allocation », « force majeure » ou « rupture » vaut tension constatée. Les résultats tirés de la série privée restent dans les champs privés ; un signal ou une hausse privée sans alerte dans les 8 semaines précédentes crée une entrée `missed_move` privée. Un signal ne valide que les conseils émis avant lui, jamais celui qu'il a lui-même provoqué.
- **Non résolu.** Verdict marché : sans prix public observé dans la fenêtre (cotation, indice ou prix douanier), le résultat est « non résolu ». Verdict personnel : sans prix observé (public ou série privée) et sans réponse de l'utilisateur dans les 14 jours, « non résolu ». Ni réussi, ni manqué : le silence ne compte jamais comme une réussite.
- **Résolu sur indice.** Un résultat obtenu sur le seul indice proxy (section 6), qui suit les intrants et non la spécialité, est marqué comme tel et compté à part. Une réponse de l'utilisateur ou la série privée l'emporte sur l'indice.
- Chaque résultat se calcule avec les seuils en vigueur le jour du conseil (entrées `settings_change`) : modifier un seuil ne réécrit jamais l'histoire.
- La correction des grands facteurs de marché vient après le MVP.

**Indicateurs** : conseils confirmés (x sur y) ; pressions de prix dans le bon sens (x sur y) ; avance moyenne ; fourchettes justes (cible 80 %, puisque [p10, p90] est un intervalle à 80 %) ; fausses alertes ; hausses manquées ; score de Brier comparé à celui du taux de base, affiché à partir de 30 hypothèses résolues ; diagramme de calibration à partir de 50. Gain estimé seulement si l'utilisateur déclare ses achats (table locale `user_purchases`) : hausse évitée moins le coût de portage du stock, présenté comme une estimation.

- Deux colonnes : « Marché » (verdicts marché et hypothèses, sur les prix publics seuls) et « Ma matière » (verdicts personnels, sur les réponses de l'utilisateur, sa série privée et les prix publics). La colonne « Ma matière » ne s'affiche que coffre ouvert.
- Chaque indicateur affiche son taux de résolution (résolus sur arrivés à échéance) et la part résolue sur indice.
- Filtre par version du modèle, du prompt et du schéma ; un changement de version est marqué sur le graphique.
- **Rodage** : pendant les 90 premiers jours, ou tant que moins de 30 conseils sont résolus, verdicts et alertes portent la mention « rodage », et l'écran Fiabilité affiche « pas encore assez de recul » au lieu des pourcentages. Un test sur des crises passées ne remplace pas le rodage : le modèle connaît déjà leur issue.

## 8. Interface

Thème sombre façon terminal de trading ; polices IBM Plex Sans et IBM Plex Mono (chiffres) ; couleurs et mise en page dans `design/`.

| Écran | Contenu |
| --- | --- |
| Marchés (accueil) | liste de suivi (matière, CAS, prix ou indice, variation, mini-courbe, verdict) ; sous chaque matière, son état de veille : « Veille active · dernière veille il y a X h », « Veille en retard » (plus de 12 h) ou « Chaîne à valider : pas de veille » ; au clic : fiche matière |
| Fiche matière | en-tête (prix, type de cotation, lien vers la chaîne, heure de la dernière vérification, mention « rodage ») ; graphique de 6 mois avec les événements marqués ; carte « Analyse IA » (verdict, pression de prix, explication, impact estimé, couverture, fiabilité, ce qui changerait l'avis ; coffre ouvert : exposition, X personnel et plan B) ; carte « Cas comparables » (section 6) ; rang de la série affichée ; bouton « Signal fournisseur » (section 11) ; liste des événements (lettre, titre, lieu, maillon, statut, sources, impact en %, horizon, probabilité) ; carte du monde (événements colorés par statut, sites de la chaîne en gris), synchronisée avec la liste ; bandeau « Goulots en préparation » |
| Chaîne | graphe et panneau de maillon (section 3) ; bascule « Chaîne type / Ma chaîne » : Ma chaîne en surimpression, épaisseur des liens selon la part de volume, maillons non utilisés estompés, encart « concentration cachée » (section 11) |
| Fiabilité | indicateurs de la section 7, en colonnes « Marché » et « Ma matière », avec taux de résolution, dont les pressions de prix ; graphique du prix réel avec les conseils datés : losange à l'émission, fourchette p10–p90 en éventail jusqu'à l'échéance, résultat juste ou manqué, hausses manquées, ruban des verdicts sous la courbe ; dossier du jour (ce que Goulot savait ce jour-là, sources, empreinte) ; registre filtrable, synchronisé avec le graphique ; diagramme de calibration ; export de l'historique (CSV, PDF) |
| Conformité | choix des matières et du modèle ; aperçu du rapport ; contrôle de complétude ; export PDF, JSON et CSV (section 10) |
| Ajouter une matière | recherche par nom ou CAS, fiche identifiée (CAS, code douanier, synonymes, sources), devis, progression des 5 étapes, couverture facultative |
| Calendrier | dates futures par matière (après le MVP si le temps manque) |
| Bibliothèque de cas | cas par matière avec leur réaction mesurée série par série ; brouillons IA à vérifier en un clic, sources sous les yeux ; préparation de nouveaux cas, avec devis (section 6) |
| Réglages | couverture par matière, seuils des verdicts et d'exposition, plafonds IA, canal d'alerte et alertes discrètes, alerte de vie externe (facultative), sources et domaines autorisés, pondération des indices proxy, coffre (clés d'accès, clé de secours, délai de verrouillage), mises à jour, page « Bilan sécurité » |
| Coffre (barre du haut, sur tous les écrans) | cadenas fermé (gris) ou ouvert (bleu, avec le temps restant) ; un clic ouvre Touch ID ou ferme le coffre ; bouton « Mode présentation » |

Navigation : Marchés, Chaîne, Fiabilité, Conformité, Calendrier, Réglages. Dans la fiche matière, la ligne « Fiabilité » ouvre l'écran Fiabilité filtré sur la matière.

Coffre fermé, chaque écran reste utilisable avec les données publiques ; un élément privé est remplacé par « motif dans le coffre » ou par un cadenas, jamais par un indice qui le trahirait (section 11). Mode présentation (touche P ou bouton) : noms de fournisseurs, parts, stocks et prix floutés, même coffre ouvert, pour partager son écran.

Verdicts : « Rien à signaler » en gris, « Problème à X sem. » en ambre (contour), « Anticiper stock » en orange (fond plein). Statuts d'événement : en cours (orange), en préparation (ambre), signal précoce (gris). La couleur n'est jamais le seul repère : toujours un libellé.

## 9. Alertes

Canal au choix : Telegram (bot en envoi seul vers la discussion du propriétaire) ou e-mail (SMTP en TLS, au seul propriétaire). Texte brut, en français, sans aucun lien : dans tout texte venu d'un article ou d'un modèle, URL, domaines et adresses e-mail sont neutralisés (`exemple[.]com`, `hxxps://`) ; sur Telegram, pas de `parse_mode` et aperçus de liens désactivés (`link_preview_options`). Le détail se consulte dans l'app.

Confidentialité : ni Telegram ni l'e-mail ne sont chiffrés de bout en bout. Aucune alerte ne contient de donnée privée : ni nom de fournisseur présenté comme tel, ni part de volume, stock, prix payé ou signal. Un verdict personnel s'annonce par la matière, le verdict, la fenêtre d'action et « motif dans l'app » : jamais X, qui, avec la fenêtre d'action, révélerait le délai contractuel. Les événements privés (signaux, sites présents seulement dans Ma chaîne) ne déclenchent jamais l'alerte « nouvel événement » : ils n'agissent que par le verdict. Option « alertes discrètes » : « Goulot : un verdict a changé, ouvre l'app », sans nom de matière.

Déclencheurs : changement de verdict ; nouvel événement confirmé de sévérité ≥ 3 ; dépense IA à 80 % et 100 % du plafond ; plafond de veille atteint pendant qu'un événement de sévérité ≥ 3 est en cours (avec la proposition de relever le plafond pour le mois, à valider dans l'app) ; mise à jour installée ou en échec ; source muette depuis plus de 24 h (une source à qui il manque sa clé le dit une seule fois) ; veille non vérifiée depuis plus de 12 h alors que le Mac est allumé ; signal fournisseur arrivé à expiration (rappel) ; événement de sécurité (échecs d'authentification répétés, signature invalide, échecs d'ouverture du coffre répétés). Regroupement : au plus une alerte par matière et par heure, sauf « Anticiper stock ». Pendant le rodage (section 7), chaque alerte de verdict se termine par « (rodage) ». Une pression de prix qui passe à +2 ou à −2 figure dans la synthèse hebdomadaire ; à elle seule, elle n'envoie pas d'alerte (réglable).

Alerte de vie externe (facultative, désactivée par défaut) : un Mac éteint ou endormi ne peut prévenir personne. À chaque cycle de collecte réussi, l'app appelle l'adresse secrète d'un service de surveillance gratuit (par exemple Healthchecks.io) ; sans appel pendant 24 heures, ce service prévient l'utilisateur par e-mail. L'appel ne contient aucune donnée ; l'adresse est rangée dans le trousseau, et son domaine n'entre dans la liste blanche qu'à l'activation, avec l'accord de l'utilisateur.

Synthèse hebdomadaire : pour chaque matière validée, lot Batch soumis le dimanche soir, envoi le lundi à 8 h ou au réveil du Mac (`prompts/digest_v1.md`, `schemas/digest_v1.json`). Le code insère lui-même le verdict, X et la fenêtre d'action ; l'IA rédige le reste. Si l'IA conteste le verdict, le message le signale sans le changer. Le code ajoute aussi le bilan de la semaine (conseils arrivés à échéance et leur résultat) et l'empreinte du registre. Pas de synthèse rédigée par l'IA en mode économie : le message se limite alors aux éléments écrits par le code.

## 10. Export de conformité

But : préparer l'évaluation des risques d'approvisionnement prévue à l'article 24 du règlement (UE) 2024/1252 sur les matières premières critiques, et répondre aux questionnaires de risque des clients. C'est une aide à la préparation : chaque page porte la mention « ne vaut ni avis juridique ni attestation de conformité ».

Cadre : les grandes entreprises (plus de 500 salariés et plus de 150 M€ de chiffre d'affaires) qui fabriquent des technologies stratégiques avec des matières premières stratégiques doivent, au moins tous les trois ans, cartographier où ces matières sont extraites, transformées et recyclées, analyser les facteurs qui peuvent affecter leur approvisionnement et évaluer leur vulnérabilité aux ruptures, puis en rendre compte à leur direction. Une révision discutée en 2026 ajouterait la cartographie des composants. Repère européen : pas plus de 65 % d'une matière stratégique venant d'un seul pays tiers. Vérifie ces points dans le texte officiel en vigueur avant de coder et note les écarts dans `docs/DECISIONS.md`.

Modèles, versionnés dans `config/compliance/` : « Article 24 » par défaut, et « Questionnaire client » générique. D'autres (devoir de vigilance) plus tard.

Contenu, par matière :

1. **Cartographie** : extraction, transformation, recyclage (maillons `mine`, `plant`, `recycler` et pays), avec parts, sources et niveau de confiance ; étape non documentée signalée comme telle.
2. **Facteurs** : concentration par étape (part du premier pays, indice de Herfindahl-Hirschman sur les parts nationales, de 0 à 10 000) ; dépassement du repère de 65 % ; contrôles d'exportation et mesures commerciales en vigueur ou annoncés ; détroits et voies traversés ; dépendances de coproduits ; événements des 12 derniers mois ; goulots à venir.
3. **Vulnérabilités** : délai X, stocks tampons, substituts, sources alternatives, couverture de l'entreprise (facultative, saisie localement).
4. **Mesures d'atténuation** : choisies par l'utilisateur dans une liste (stock de sécurité, second fournisseur, contrat-cadre, substitution, autre, avec commentaire) ; jamais rédigées à sa place.
5. **Sources et méthode** : liste des sources, méthode de calcul, date de la chaîne utilisée, résumé de fiabilité tiré du registre, nom et fonction du signataire saisis par l'utilisateur.

Base de la cartographie, au choix : chaîne type (par défaut) ou Ma chaîne, plus proche de ce que l'article 24 demande, puisqu'il porte sur l'approvisionnement de l'entreprise. Avec Ma chaîne, l'export exige le coffre ouvert, chaque page porte « Confidentiel », et les fichiers sont produits à la volée, sans copie en clair sur le Mac : seule leur empreinte est conservée. La couverture de l'entreprise et les mesures d'atténuation, chiffrées, exigent aussi le coffre ouvert.

Production :

- PDF par la mise en page d'impression du navigateur (feuille de style dédiée, aucune nouvelle dépendance) ; annexes JSON et CSV produites par le serveur.
- Aucun appel à l'IA : calculs et textes viennent du code, à partir des données déjà validées. Rien ne quitte le Mac.
- Chaque export porte son empreinte SHA-256 en pied de page et crée une entrée `export` au registre.
- Contrôle avant export : sections complètes, chaîne validée depuis moins de 12 mois, mesures renseignées. Sinon, le rapport porte la mention « brouillon » sur chaque page.

## 11. Ma chaîne et coffre

But : affiner les verdicts avec la chaîne réelle de l'utilisateur, sans que personne d'autre ne puisse la lire : ni l'IA, ni les sources interrogées, ni un intrus devant le tableau de bord, ni quelqu'un qui récupère le disque ou une sauvegarde. Principe : l'IA juge le marché, le Mac juge l'exposition de l'utilisateur.

### Contenu de Ma chaîne

Un document par matière, chiffré, en révisions successives (`private_chain`).

| Élément | Champs |
| --- | --- |
| Site | nom, rôle (producteur, transformateur, distributeur, entrepôt, port), entreprise, alias (dont la langue locale), ville ou point sur la carte, pays ; maillon de la chaîne type correspondant, s'il existe ; niveau de surveillance en ligne (aléas seulement ; ville et matière, par défaut ; ville, matière et entreprise) |
| Lien | origine, destination, part (%) : part des achats de la destination qui viennent de l'origine (pour un lien vers l'utilisateur, part de son volume), mode de transport, incoterm, délai contractuel ou constaté (jours) |
| Stocks | chez l'utilisateur, chez le fournisseur (s'il le communique), en transit ; en jours |
| Alternatives | site ou fournisseur, statut (qualifié, en cours de qualification, non qualifié), délai pour basculer |
| Notes | texte libre |

Trois champs suffisent pour commencer : le site, la part du volume, le délai.

### Saisie

- Sur la chaîne type, un maillon touché propose « C'est mon fournisseur », « Je ne passe pas par là » ou « Ajouter un site absent ».
- Import CSV : modèle téléchargeable depuis l'app, colonnes en français ; fichier lu par la page et envoyé comme texte dans un corps JSON, 1 Mo au plus (un envoi de fichier passerait par un fichier temporaire), jamais écrit sur le disque ; aperçu et validation avant enregistrement.
- Localisation hors ligne : liste embarquée des villes de plus de 15 000 habitants (GeoNames, licence CC BY 4.0, installée par `goulot dev fetch-cities` : noms en alphabet latin et en écritures chinoise, japonaise et coréenne), ou point placé sur la carte. Aucun service de géocodage en ligne : le nom des fournisseurs ne part nulle part.
- Contrôles : parts entre 0 et 100 %, somme des parts entrantes de chaque site au plus égale à 100 %, chemin jusqu'à la matière, délais positifs. Un écart avec la chaîne type est signalé, jamais corrigé à la place de l'utilisateur.
- La première saisie exige un coffre configuré : clé de secours confirmée et clé d'accès enregistrée.

### Ce que Ma chaîne change (sur le Mac, sans IA)

- **Rapprochement** : les entités et le lieu extraits au tri (`entities`, `place`, `country_iso2`) sont comparés aux noms, alias et villes des sites de Ma chaîne ; un aléa à moins de 50 km d'un site le touche. Les liens trouvés vont, chiffrés, dans `private_links`.
- **Exposition** : pour un site de Ma chaîne, la somme, sur tous ses chemins jusqu'à l'utilisateur, du produit des parts le long de chaque chemin. L'écran montre ce flux : la part du volume de l'utilisateur qui passe par chaque lien. En amont d'un site relié à un maillon de la chaîne type, l'exposition se répartit selon les parts de la chaîne type ; en amont d'un site non relié, le fournisseur est supposé s'approvisionner comme le marché (parts mondiales de chaque niveau), hypothèse affichée.
- **Déclenchement** : un site qui pèse pour l'utilisateur (au moins 10 % de son volume, ou son fournisseur unique) déclenche l'analyse, même sous 0,5 % de l'offre mondiale (section 4).
- **Verdict personnel** : règle physique sur ses chemins, avec la sévérité `s_perso` relevée selon l'exposition ; X et W sur ses stocks et délais ; règle de prix inchangée (section 5).
- **Concentration cachée** : part du premier pays et indice de Herfindahl-Hirschman, comparés entre la chaîne type et Ma chaîne (« le marché est diversifié, toi non »).
- **Plan B** : pour un événement sur un de ses chemins, rappel des alternatives qualifiées et de leur délai de bascule.

### Signal fournisseur

Bouton de la fiche matière : délai allongé (ancien et nouveau délai), hausse annoncée (pourcentage et date d'effet), allocation, force majeure, rupture, retour à la normale ; site concerné, date, commentaire. Chaque signal est chiffré dans `user_signals`, inscrit au registre (entrée `signal`, champs privés), compté dans le verdict personnel (section 5) et dans les résultats (section 7). Jamais envoyé à l'IA.

### Ce qui ne sort jamais

Ma chaîne, les signaux, les liens privés, la couverture, les devis, les achats et les mesures d'atténuation n'apparaissent ni dans un prompt (constructeurs à liste blanche), ni dans une alerte, ni dans les journaux, ni dans un export public. Vers les sources ne part que le niveau de surveillance choisi pour chaque site : rien pour « aléas seulement » (zone comparée sur le Mac), la ville et la matière par défaut, le nom de l'entreprise seulement sur autorisation, jamais le fait qu'il s'agit d'un fournisseur de l'utilisateur. Ces règles sont construites en mémoire à chaque cycle ; un article trouvé ainsi est gardé sans la règle qui l'a trouvé. Un événement trouvé seulement par Ma chaîne (vue `public_events`) n'entre ni dans le verdict marché, ni dans la synthèse, ni dans les alertes « nouvel événement ». La CLI ne lit jamais les données privées : un export fondé sur Ma chaîne se fait depuis l'écran Conformité, coffre ouvert. La chaîne type continue d'alimenter l'IA comme avant. Des valeurs canari dans les jeux de test le vérifient (section 13).

### Coffre

- **Données chiffrées** : `private_chain`, `user_signals`, `private_links`, `user_coverage`, `user_quotes`, `user_purchases`, `mitigations`, entrées du calcul des verdicts, X, fenêtre d'action et explication des verdicts personnels, champs privés du registre. Les règles de veille issues de Ma chaîne ne sont jamais stockées (section 3).
- **Chiffrement** : AES-256-GCM (bibliothèque `cryptography`, déjà dans la stack), nonce aléatoire de 96 bits par enregistrement, données associées liant chaque chiffré à un nom logique stable de champ, à l'identifiant unique (UUID) de son enregistrement et à sa version de clé : un chiffré déplacé ne se déchiffre plus, et une migration ou un `VACUUM` ne cassent rien.
- **Clés** : une clé maîtresse de 256 bits, dans le trousseau de session macOS (élément `goulot-vault`, hors du service `goulot`, non synchronisé), enveloppe des clés de données versionnées, stockées dans `vault_keys`, ainsi que la clé HMAC du registre. Une copie de la clé maîtresse est enveloppée par la clé de secours. Une nouvelle clé de données ne rechiffre jamais le registre. La clé maîtresse n'est jamais dans la base ni dans les sauvegardes de l'app ; le trousseau de session, lui, est sauvegardé par Time Machine et protégé par le mot de passe du Mac : la sauvegarde Time Machine doit être chiffrée et le mot de passe du Mac solide.
- **Clé de secours** : 128 bits aléatoires, affichés une seule fois par `goulot setup` en groupes de caractères avec un caractère de contrôle (base 32 de Crockford), à imprimer ou à ranger dans un gestionnaire de mots de passe ; l'utilisateur la retape en entier pour confirmer. `goulot vault new-recovery-key` en émet une nouvelle, coffre ouvert, et invalide l'ancienne. Avec une sauvegarde de la base faite hors du Mac (Time Machine chiffrée), elle permet `goulot vault recover` sur un nouveau Mac. Sans elle, ou sans sauvegarde hors du Mac, la perte du Mac emporte les données privées : l'app le dit clairement à la création.
- **Ouverture** : clé d'accès (passkey) WebAuthn, liée à Touch ID ou au mot de passe du Mac ; `rpId` `localhost`, vérification de l'utilisateur obligatoire, sans attestation ; vérification côté serveur : la bibliothèque `webauthn` (Duo Labs) contrôle la signature, l'origine exacte `http://localhost:8742`, le `rpId` et le drapeau de vérification ; l'app garde elle-même le défi à usage unique et le compteur de signatures. WebAuthn refuse les adresses IP : l'interface est donc servie sur `http://localhost:8742` (`docs/SECURITY.md`). Une ouverture réussie donne un jeton de coffre de 256 bits, lié à la session, gardé en mémoire par le frontend et exigé dans l'en-tête `X-Vault-Token` sur toute route `/api/private/`.
- **Fermeture** : 15 minutes sans action de l'utilisateur (réglable de 5 à 60) : l'interrogation périodique de l'interface n'appelle que des routes hors de `/api/private/` et ne prolonge rien ; durée absolue de 8 heures ; mise en veille du Mac (saut d'horloge détecté), onglet masqué plus de 5 minutes, cadenas, `goulot vault lock`, `goulot panic`. Les clés déchiffrées ne restent en mémoire que le temps d'un calcul.
- **Enrôlement** : la première clé d'accès exige la clé de secours, pour qu'un intrus devant le Mac ne puisse pas enregistrer la sienne avant le propriétaire ; une clé supplémentaire exige une signature fraîche (moins de 2 minutes) d'une clé existante, ou la clé de secours ; une nouvelle clé après perte, la clé de secours. Chaque enregistrement envoie une alerte et apparaît dans Réglages, où l'utilisateur peut le révoquer.
- **Vue coffre fermé** : chaîne type, verdicts (libellé et fenêtre d'action ; X seulement pour le verdict marché), événements publics, prix. Aucune marque « ta chaîne », aucun motif personnel (remplacé par « motif dans le coffre »), aucun site de Ma chaîne ; un événement relié seulement à Ma chaîne n'apparaît que coffre ouvert. Les routes hors de `/api/private/` ne renvoient rien d'autre qui dépende de Ma chaîne.
- **Mode standard (MVP)** : la veille de fond lit la clé maîtresse dans le trousseau pour calculer le verdict personnel même coffre fermé ; le coffre protège l'affichage, la saisie et les exports. Limite : quelqu'un qui utilise la session ouverte avec un Terminal peut lire la clé du trousseau comme le fait l'app. Le coffre protège le tableau de bord, pas une session laissée ouverte : verrouiller l'écran reste indispensable, coffre fermé ou non. Si une mise à jour fait redemander l'accès au trousseau, la veille continue avec le verdict marché seul et l'app affiche la demande à valider.
- **Mode renforcé (après le MVP)** : la clé maîtresse quitte le trousseau ; elle est enveloppée par une clé dérivée de l'extension PRF de la clé d'accès (prise en charge annoncée par Safari 18, Chrome 132 et Firefox 139 ou plus récents, sous macOS 15 ou plus récent ; à revérifier). Rien de privé n'est lisible sans Touch ID ; coffre fermé, la veille de fond calcule le verdict marché, et le verdict personnel est recalculé à l'ouverture.
- **Effacement** : `PRAGMA secure_delete = ON`. Aucune donnée privée n'est jamais écrite en clair, donc aucune migration de déchiffrement ; un changement de format des données privées se fait par l'app, coffre ouvert.
- **Limites** : un programme qui tourne sous la session de l'utilisateur peut lire les données privées pendant que le coffre est ouvert, et à tout moment en mode standard. Parade : verrouillage immédiat de l'écran, FileVault, macOS à jour (`docs/SECURITY.md`). Les tuiles de la carte révèlent à OpenFreeMap la zone affichée. Les articles publics collectés pour les sites de Ma chaîne restent en clair dans la base, comme tous les articles : leur lien avec Ma chaîne est chiffré, mais quelqu'un qui volerait la base pourrait deviner qu'une zone intéresse l'utilisateur.
- **Version hébergée (hors MVP)** : même principe dans le navigateur : chiffrement de bout en bout avec une clé dérivée de la clé d'accès, verdict personnel calculé dans le navigateur ; le serveur ne voit que le marché.

## 12. Hors périmètre du MVP

Globe 3D, AIS en temps réel, modèle open source local, accès depuis le téléphone, calendrier avancé, correction des facteurs de marché, multi-utilisateur, mode renforcé du coffre, version hébergée chiffrée de bout en bout, signaux de demande (ruées, stockage de précaution, structure des contrats à terme, stocks des bourses), import Excel natif, gaz européen (TTF) au jour le jour de source officielle et gratuite (aucune connue : moyenne mensuelle de la Banque mondiale, ou dépannage).

## 13. Jeux de test

- `tests/fixtures/articles/` : articles synthétiques en plusieurs langues, dont des doublons repris par plusieurs médias, des articles hors sujet et des articles piégés (consignes adressées à une IA, balises HTML, liens vers des adresses internes).
- `tests/fixtures/llm/` : réponses IA enregistrées, pour rejouer tout le pipeline sans appel payant (`goulot replay`).
- `tests/fixtures/chains/` : chaînes de référence pour les 3 CAS de test. Jamais de CAS, de chaîne ou de réponse IA propres à l'utilisateur dans le dépôt.
- `schemas/examples/` : un exemple fictif par schéma, point de départ des réponses enregistrées.
- `tests/fixtures/ledger/` : un registre fictif avec une entrée modifiée et une entrée supprimée, que `goulot ledger verify` doit détecter ; des séries de prix fictives avec réussites, fausses alertes et hausses manquées connues d'avance, pour tester les règles de résultat ; un conseil sans prix ni réponse, qui doit finir « non résolu », et un conseil résolu sur le seul indice, qui doit être marqué comme tel.
- `tests/fixtures/history/` : deux séries fictives (une matière et son facteur de marché) et quatre cas fictifs, dont deux qui se chevauchent. La variation propre calculée doit égaler la valeur attendue ; les cas chevauchants sortent de la médiane ; un brouillon IA n'y entre pas ; avec moins de 3 cas, l'écran affiche « pas assez de cas comparables » ; un rejeu au 1er juin n'utilise aucun cas annoncé après cette date. Une matière sans aucune série reçoit quand même une pression de prix, sans pourcentage.
- `tests/fixtures/private/` : une Ma chaîne fictive pour un des 3 CAS de test, avec des valeurs canari reconnaissables octet par octet (par exemple un site `CANARY-SITE-7F3A`, une ville `CANARY-CITY-9Q2X`, une part de 63,417 %, un stock de 37,29 jours, un devis à 4 217,93). Aucun canari ne doit apparaître dans une requête sortante, un prompt, une alerte, un journal, un export public, ni en clair dans le fichier de la base, son journal WAL ou une sauvegarde.
- Cas « Ma chaîne » : un fournisseur privé qui pèse 0,3 % de l'offre mondiale et 80 % du volume de l'utilisateur, touché par un incendie, déclenche l'analyse et un verdict personnel ; une panne sur un site hors de ses chemins ne déclenche pas la règle physique personnelle ; un signal « hausse annoncée de 8 % au 1er décembre », saisi le 3 novembre, passe la matière en « Anticiper stock » avec X = 4 semaines ; un signal « force majeure » chez un fournisseur amont donne pour X le délai de ce site jusqu'à l'utilisateur, pas 0 ; ce signal ne valide pas le conseil qu'il a provoqué.
- Cas fonctionnel « Yichun 2025 » : le 24 juillet 2025, problèmes de permis signalés dans huit mines de lithium de Yichun ; le 9 août, arrêt de la mine de CATL à Jianxiawo ; le 11 août, carbonate de lithium au plafond quotidien de +8 % à Guangzhou. Le rejeu doit passer la matière au moins en « Problème potentiel » dès le 24 juillet. C'est un test de fonctionnement, pas de performance : le modèle connaît déjà l'issue.
