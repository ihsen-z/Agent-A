/* Interface de rapprochement : JavaScript sans dependance ni build.
   Toute la logique metier reste en Python : ce fichier ne fait qu'afficher
   l'analyse renvoyee par /api/analyse et transmettre les actions. */
"use strict";

// -- etat ------------------------------------------------------------------

const etat = {
  analyse: null,
  filtres: { statut: "tous", dossier: "tous", recherche: "" },
  tri: { col: "date", sens: 1 },
  relance: null,
  fichiers: {},
  // Vrai quand le resultat vient d'etre calcule (lancement, import, regle
  // modifiee) : le prochain rendu revele les donnees, une seule fois.
  reveler: false,
};

const STATUTS = {
  manquant: { libelle: "Pièce manquante", filtre: "Manquantes", classe: "manquant" },
  a_verifier: { libelle: "À vérifier", filtre: "À vérifier", classe: "verifier" },
  partiel: { libelle: "Paiement partiel", filtre: "Partielles", classe: "partiel" },
  justifie: { libelle: "Justifiée", filtre: "Justifiées", classe: "justifie" },
  hors_perimetre: { libelle: "Sans pièce nécessaire", filtre: "Sans pièce nécessaire", classe: "hors" },
};
const ORDRE_REPARTITION = ["hors_perimetre", "justifie", "a_verifier", "partiel", "manquant"];
const ORDRE_FILTRES = ["manquant", "a_verifier", "partiel", "justifie", "hors_perimetre"];

const REGLES = {
  salaires: "Salaires",
  charges_sociales: "Charges sociales",
  impots_taxes: "Impôts et taxes",
  frais_bancaires: "Frais bancaires",
  emprunts: "Échéances d'emprunt",
  mouvements_internes: "Virements internes",
  encaissements_clients: "Encaissements clients",
  rejets_et_annulations: "Rejets et annulations",
  appariement_1_1: "Rapprochement direct",
  acompte: "Acompte",
  aucun_candidat: "Aucune pièce reçue",
  credit_non_identifie: "Encaissement non identifié",
};

const TONS = {
  courtois: "Ton courtois",
  direct: "Ton direct",
  urgent: "Urgent : échéance TVA proche",
};

const FICHIERS_IMPORT = [
  { cle: "releve", nom: "releve_bancaire.csv", titre: "Relevé bancaire", obligatoire: true },
  { cle: "pieces", nom: "pieces_recues.csv", titre: "Pièces reçues", obligatoire: true },
  { cle: "dossiers", nom: "dossiers.csv", titre: "Dossiers clients", obligatoire: true },
  { cle: "alias", nom: "alias_fournisseurs.csv", titre: "Alias fournisseurs" },
  { cle: "exclusions", nom: "exclusions_dossier.csv", titre: "Règles apprises" },
  { cle: "verite", nom: "verite_terrain.csv", titre: "Vérité terrain", aide: "pour mesurer la qualité" },
];

// -- icones (SVG en ligne, aucune ressource externe) -------------------------

const ICONES = {
  accueil: '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/><path d="M10 21v-6h4v6"/>',
  tableau: '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
  operations: '<path d="M9 6h12M9 12h12M9 18h12"/><path d="M4 6h.01M4 12h.01M4 18h.01"/>',
  relances: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/>',
  regles: '<path d="M4 19.5V5a2 2 0 0 1 2-2h14v16H6a2 2 0 0 0-2 2z"/><path d="M8 7h8M8 11h6"/>',
  qualite: '<path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6z"/><path d="m9 12 2 2 4-4"/>',
  upload: '<path d="M12 15V3"/><path d="m7 8 5-5 5 5"/><path d="M5 21h14"/>',
  download: '<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/>',
  play: '<path d="M7 4.5v15l12-7.5z"/>',
  copie: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/>',
  fermer: '<path d="M18 6 6 18M6 6l12 12"/>',
  recherche: '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>',
  check: '<path d="m5 12 5 5 9-10"/>',
  croix: '<path d="M18 6 6 18M6 6l12 12"/>',
  envoi: '<path d="m22 2-7 20-4-9-9-4z"/><path d="M22 2 11 13"/>',
  fichier: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/>',
  alerte: '<circle cx="12" cy="12" r="9"/><path d="M12 8v4.5M12 16h.01"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
  groupe: '<rect x="3" y="4" width="8" height="7" rx="1.5"/><rect x="13" y="4" width="8" height="7" rx="1.5"/><rect x="8" y="13" width="8" height="7" rx="1.5"/>',
  acompte: '<circle cx="12" cy="12" r="9"/><path d="M12 3v9h9"/>',
  apprise: '<path d="M4 19.5V5a2 2 0 0 1 2-2h14v16H6a2 2 0 0 0-2 2z"/><path d="m9 10 2 2 4-4"/>',
  contre_exemple: '<path d="M12 3 2 20h20z"/><path d="M12 10v4M12 17h.01"/>',
  arrondi: '<path d="M4 12h16"/><path d="M8 8h.01M16 16h.01"/>',
  poubelle: '<path d="M4 7h16M10 11v6M14 11v6"/><path d="M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  remise: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>',
  cadenas: '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
  oeil: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  tri: '<path d="m7 15 5 5 5-5M7 9l5-5 5 5"/>',
};
const ico = (nom) => `<svg viewBox="0 0 24 24" aria-hidden="true">${ICONES[nom] || ""}</svg>`;

// -- formats -----------------------------------------------------------------

const fmtEur = new Intl.NumberFormat("fr-FR", { style: "currency", currency: "EUR" });
const fmtNb = new Intl.NumberFormat("fr-FR");
const eur = (v, devise = "EUR") =>
  devise === "EUR" ? fmtEur.format(Number(v)) : `${fmtNb.format(Number(v))} ${devise}`;
const pct = (v, dec = 1) =>
  `${new Intl.NumberFormat("fr-FR", { minimumFractionDigits: dec, maximumFractionDigits: dec }).format(v)} %`;
const nb = (v) => fmtNb.format(v);
const dateFr = (iso) => (iso ? iso.slice(0, 10).split("-").reverse().join("/") : "");
const pluriel = (n, sing, plur) => `${nb(n)} ${n > 1 ? plur || sing + "s" : sing}`;
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const pause = (ms) => new Promise((r) => setTimeout(r, ms));
const moinsDeMouvement = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

function periode(p) {
  if (!p || !p.debut) return "—";
  const d = new Date(p.debut + "T00:00:00");
  const f = new Date(p.fin + "T00:00:00");
  const mois = (x) => new Intl.DateTimeFormat("fr-FR", { month: "long" }).format(x);
  if (d.getFullYear() === f.getFullYear()) {
    if (d.getMonth() === f.getMonth()) return `${mois(d)} ${d.getFullYear()}`;
    return `${mois(d)} – ${mois(f)} ${f.getFullYear()}`;
  }
  return `${mois(d)} ${d.getFullYear()} – ${mois(f)} ${f.getFullYear()}`;
}

function dateHeure(iso) {
  const d = new Date(iso);
  return `${d.toLocaleDateString("fr-FR")} à ${d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })}`;
}

function joursAvant(iso) {
  const aujourdhui = new Date();
  aujourdhui.setHours(0, 0, 0, 0);
  return Math.round((new Date(iso + "T00:00:00") - aujourdhui) / 86400000);
}

function libelleRegle(regle) {
  if (!regle) return "";
  if (regle.startsWith("apprise:")) return "Règle du cabinet";
  const groupe = regle.match(/^paiement_groupe_(\d+)$/);
  if (groupe) return `Paiement groupé (${groupe[1]} factures)`;
  return REGLES[regle] || regle;
}

const badge = (statut) =>
  `<span class="badge s-${STATUTS[statut]?.classe || "hors"}">${esc(STATUTS[statut]?.libelle || statut)}</span>`;

const raisonSociale = (code) =>
  etat.analyse?.par_dossier.find((d) => d.code === code)?.raison_sociale || code;

// -- echanges avec le serveur local -------------------------------------------

async function api(chemin, corps) {
  const options = corps === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(corps),
  };
  let reponse;
  try {
    reponse = await fetch(chemin, options);
  } catch {
    throw new Error("Le serveur local ne répond pas. Relancez l'interface (Demarrer_interface.bat).");
  }
  let donnees;
  try {
    donnees = await reponse.json();
  } catch {
    throw new Error(`Réponse illisible du serveur (code ${reponse.status}).`);
  }
  if (!reponse.ok) throw new Error(donnees.erreur || `Erreur ${reponse.status}`);
  return donnees;
}

function toast(message, genre = "ok") {
  const zone = document.getElementById("toasts");
  const el = document.createElement("div");
  el.className = `toast ${genre}`;
  el.innerHTML = `${ico(genre === "erreur" ? "alerte" : "check")}<span>${esc(message)}</span>`;
  zone.appendChild(el);
  setTimeout(() => {
    if (moinsDeMouvement()) return el.remove();
    el.classList.add("sort");
    setTimeout(() => el.remove(), 190);
  }, genre === "erreur" ? 7000 : 4200);
}

// -- theme clair / sombre ---------------------------------------------------------

function choixTheme() {
  const t = document.documentElement.dataset.theme;
  return t === "light" || t === "dark" ? t : "auto";
}

function majBoutonsTheme() {
  document.querySelectorAll("[data-theme-choix]").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.themeChoix === choixTheme())));
}

function appliquerTheme(choix) {
  if (choix === choixTheme()) return;
  const basculer = () => {
    if (choix === "auto") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = choix;
    try {
      if (choix === "auto") localStorage.removeItem("theme");
      else localStorage.setItem("theme", choix);
    } catch { /* stockage indisponible : le choix vaut pour cette page */ }
    majBoutonsTheme();
  };
  // Fondu enchaine du document entier quand le navigateur sait le faire.
  if (document.startViewTransition && !moinsDeMouvement()) document.startViewTransition(basculer);
  else basculer();
}

// -- mouvement : revelation des donnees ---------------------------------------------

let generationRendu = 0;
const sortieExpo = (x) => (x >= 1 ? 1 : 1 - Math.pow(2, -10 * x));

// Compteurs des indicateurs : de 0 a la valeur, 700 ms, deceleration. Le texte
// final est deja dans le DOM ; l'animation est annulee par tout nouveau rendu.
function animerCompteurs(racine) {
  if (moinsDeMouvement()) return;
  const generation = generationRendu;
  racine.querySelectorAll("[data-compteur]").forEach((el) => {
    const cible = Number(el.dataset.compteur);
    const final = el.textContent;
    const format = el.dataset.format === "pct" ? (v) => pct(v) : (v) => nb(Math.round(v));
    const debut = performance.now() + 120;
    el.textContent = format(0);
    const image = (maintenant) => {
      if (generation !== generationRendu) { el.textContent = final; return; }
      const avance = Math.max(0, (maintenant - debut) / 700);
      if (avance >= 1) { el.textContent = final; return; }
      el.textContent = format(cible * sortieExpo(avance));
      requestAnimationFrame(image);
    };
    requestAnimationFrame(image);
  });
}

// Indices d'echelonnement (--i) des blocs, des cartes et des barres.
function indexer(racine) {
  [...racine.children].forEach((el, i) => el.style.setProperty("--i", i));
  racine.querySelectorAll(".kpis").forEach((g) => [...g.children].forEach((el, i) => el.style.setProperty("--i", i)));
  racine.querySelectorAll(".barres").forEach((g) =>
    g.querySelectorAll(".rail > span").forEach((el, i) => el.style.setProperty("--i", i)));
}

function fondu(el, decalage = 4) {
  if (!el || moinsDeMouvement() || !el.animate) return;
  el.animate(
    [{ opacity: 0, transform: `translateY(${decalage}px)` }, { opacity: 1, transform: "none" }],
    { duration: 220, easing: "cubic-bezier(.16, 1, .3, 1)" },
  );
}

// -- navigation ----------------------------------------------------------------

const VUES = {
  accueil: { titre: "Accueil", icone: "accueil", rendu: vueAccueil },
  tableau: { titre: "Tableau de bord", icone: "tableau", rendu: vueTableau },
  operations: { titre: "Opérations", icone: "operations", rendu: vueOperations },
  relances: { titre: "Relances", icone: "relances", rendu: vueRelances },
  regles: { titre: "Règles du cabinet", icone: "regles", rendu: vueRegles },
  qualite: { titre: "Qualité mesurée", icone: "qualite", rendu: vueQualite },
};

function route() {
  const [nom, ...reste] = location.hash.replace(/^#\/?/, "").split("/");
  return { nom: VUES[nom] ? nom : "accueil", param: reste.map(decodeURIComponent).join("/") };
}

function rendreNav(courante) {
  const a = etat.analyse;
  const compteurs = {
    operations: a ? `<span class="compteur alerte" title="Pièces manquantes">${a.stats.manquant}</span>` : "",
    relances: a ? `<span class="compteur">${a.relances.length}</span>` : "",
  };
  document.getElementById("nav").innerHTML = Object.entries(VUES)
    .map(([nom, v]) => `<a href="#/${nom}" ${nom === courante ? 'aria-current="page"' : ""}>${ico(v.icone)}<span>${v.titre}</span>${compteurs[nom] || ""}</a>`)
    .join("");
}

function rendreSource() {
  const a = etat.analyse;
  const zone = document.getElementById("source");
  if (!a) { zone.innerHTML = ""; return; }
  const demo = a.source === "demo";
  zone.innerHTML = `
    <div class="source-carte">
      <div class="source-titre"><span class="pastille ${demo ? "" : "import"}"></span>
        <span>${demo ? "Données de démonstration" : "Fichiers importés"}
          <small>${pluriel(a.volumes.dossiers, "dossier")} · ${esc(periode(a.periode))}</small></span>
      </div>
      <div class="source-actions">
        <button class="btn btn-petit" data-action="importer">${ico("upload")} Importer</button>
        <button class="btn btn-petit" data-action="revenir-demo" title="Recharger les données de démonstration et oublier les règles ajoutées">${ico("remise")} Réinitialiser</button>
      </div>
    </div>`;
}

async function rendre() {
  const { nom, param } = route();
  fermerCouche();
  rendreNav(nom);
  const vue = document.getElementById("vue");
  if (!etat.analyse && nom !== "accueil") {
    vue.innerHTML = '<p class="chargement">Chargement…</p>';
    await charger();
  }
  generationRendu += 1;
  vue.classList.remove("revele", "change");
  vue.innerHTML = VUES[nom].rendu(param);
  document.title = `${VUES[nom].titre} · Relance des justificatifs`;
  rendreSource();
  rendreNav(nom);
  apresRendu(nom);
  void vue.offsetWidth; // relance l'animation CSS sur le nouveau contenu
  if (etat.reveler && nom !== "accueil") {
    indexer(vue);
    vue.classList.add("revele");
    animerCompteurs(vue);
    etat.reveler = false;
  } else {
    vue.classList.add("change");
  }
}

function apresRendu(nom) {
  if (nom === "operations") majTableOperations();
}

async function charger() {
  try {
    etat.analyse = await api("/api/analyse");
  } catch (e) {
    toast(e.message, "erreur");
  }
}

// -- vue : accueil ------------------------------------------------------------------

function vueAccueil() {
  const a = etat.analyse;
  const demo = !a || a.source === "demo";
  const volumes = a ? `
    <dl class="volumes">
      <div><dt>Dossiers clients</dt><dd>${nb(a.volumes.dossiers)}</dd></div>
      <div><dt>Opérations bancaires</dt><dd>${nb(a.volumes.operations)}</dd></div>
      <div><dt>Pièces reçues</dt><dd>${nb(a.volumes.pieces)}</dd></div>
      <div><dt>Période</dt><dd class="texte">${esc(periode(a.periode))}</dd></div>
    </dl>` : "";
  return `
    <section class="accueil">
      <div class="carte accueil-carte">
        <p class="surtitre">Rapprochement du mois</p>
        <h1>Les justificatifs qui manquent, trouvés et réclamés en quelques secondes</h1>
        <p class="intro">Le relevé bancaire est comparé aux pièces reçues. Les opérations qui n'exigent aucune facture sont écartées, les autres sont rapprochées au centime près, et une seule relance est préparée pour chaque client.</p>
        <div class="ligne-source">
          <span class="etiquette ${demo ? "direct" : "courtois"}">${demo ? "Données de démonstration" : "Vos fichiers importés"}</span>
          ${demo ? '<span class="compte-lignes">3 PME françaises fictives, relevés réalistes</span>' : ""}
        </div>
        ${volumes}
        <div class="actions">
          <button class="btn btn-primaire btn-grand" data-action="lancer">${ico("play")} Lancer le rapprochement</button>
          <button class="btn btn-secondaire btn-grand" data-action="importer">${ico("upload")} Importer vos fichiers</button>
        </div>
        <div id="etapes"></div>
      </div>
      <div class="pile">
        <ol class="parcours">
          <li><strong>Relevé bancaire et pièces reçues</strong><span>L'export CSV de la banque et l'inventaire des factures déjà transmises par le client.</span></li>
          <li><strong>Tri et rapprochement automatiques</strong><span>Salaires, URSSAF, impôts et frais bancaires écartés ; chaque paiement rapproché de sa facture.</span></li>
          <li><strong>Relances prêtes à envoyer</strong><span>Un e-mail par client regroupant toutes les pièces manquantes, relu par le comptable avant envoi.</span></li>
        </ol>
        <div class="carte">
          <div class="garanties">
            <div class="garantie">${ico("oeil")}<span><strong>Auditable</strong>Chaque décision porte le nom de sa règle.</span></div>
            <div class="garantie">${ico("cadenas")}<span><strong>Local</strong>Aucune donnée ne quitte le poste.</span></div>
            <div class="garantie">${ico("qualite")}<span><strong>Prudent</strong>Dans le doute, la pièce est réclamée.</span></div>
          </div>
        </div>
      </div>
    </section>`;
}

async function lancer(bouton) {
  bouton.disabled = true;
  const zone = document.getElementById("etapes");
  let a;
  try {
    a = await api("/api/analyse");
  } catch (e) {
    toast(e.message, "erreur");
    bouton.disabled = false;
    return;
  }
  etat.analyse = a;
  const s = a.stats;
  const etapes = [
    ["Lecture du relevé bancaire", pluriel(s.operations, "opération")],
    ["Tri des opérations sans facture", pluriel(s.hors_perimetre, "écartée")],
    ["Rapprochement avec les pièces reçues", `${pluriel(s.justifie, "justifiée")}${s.a_verifier ? ` · ${nb(s.a_verifier)} à vérifier` : ""}`],
    ["Préparation des relances", `${pluriel(a.relances.length, "e-mail")} · ${pluriel(s.manquant, "pièce")}`],
  ];
  zone.innerHTML = `<ol class="etapes">${etapes
    .map(([titre, detail]) => `<li><span class="rond">${ico("check")}</span><span>${esc(titre)}</span><span class="detail">${esc(detail)}</span></li>`)
    .join("")}</ol>`;
  const delai = moinsDeMouvement() ? 0 : 420;
  for (const li of zone.querySelectorAll("li")) {
    li.classList.add("en-cours");
    await pause(delai);
    li.classList.replace("en-cours", "fait");
  }
  await pause(delai);
  etat.reveler = true;
  location.hash = "#/tableau";
}

// -- vue : tableau de bord ----------------------------------------------------------

function vueTableau() {
  const a = etat.analyse;
  if (!a) return "";
  const s = a.stats;
  const auto = s.hors_perimetre + s.justifie;
  const reste = [
    s.manquant && `${nb(s.manquant)} à relancer`,
    s.a_verifier && `${nb(s.a_verifier)} à vérifier`,
    s.partiel && pluriel(s.partiel, "partiel"),
  ].filter(Boolean).join(" · ") || "Rien à traiter";

  const segments = ORDRE_REPARTITION.filter((st) => s[st] > 0).map((st) =>
    `<span class="f-${STATUTS[st].classe}" style="flex:${s[st]} 1 0" title="${esc(STATUTS[st].libelle)} : ${nb(s[st])}"></span>`).join("");
  const legende = ORDRE_REPARTITION.map((st) => `
    <li><button data-action="filtrer" data-statut="${st}">
      <span class="nom"><span class="pt f-${STATUTS[st].classe}"></span>${esc(STATUTS[st].libelle)}</span>
      <span class="val">${nb(s[st])}<small>${pct(100 * s[st] / Math.max(s.operations, 1))}</small></span>
    </button></li>`).join("");

  const maxRegle = Math.max(1, ...a.par_regle.map((r) => r.operations));
  const barres = a.par_regle.map((r) => `
    <li title="${esc(r.motif)}">
      <span class="nom">${esc(libelleRegle(r.regle))}</span>
      <span class="rail ${r.regle.startsWith("apprise:") ? "apprise" : ""}"><span style="width:${(100 * r.operations / maxRegle).toFixed(1)}%"></span></span>
      <span class="n">${nb(r.operations)}</span>
    </li>`).join("");

  const faits = a.faits_marquants.length
    ? `<ul class="faits">${a.faits_marquants.map((f) => `
        <li><button data-action="ouvrir-op" data-ref="${esc(f.reference)}">
          <span class="ico">${ico(f.genre)}</span>
          <span><strong>${esc(f.titre)}</strong><span class="txt">${esc(f.detail)}</span></span>
        </button></li>`).join("")}</ul>`
    : '<p class="compte-lignes">Aucun cas particulier dans ce relevé.</p>';

  const lignesDossiers = a.par_dossier.map((d) => {
    const ech = d.echeance_tva ? joursAvant(d.echeance_tva) : null;
    return `
    <tr>
      <td class="cellule-dossier"><strong>${esc(d.raison_sociale)}</strong><small>${esc(d.code)}</small></td>
      <td class="num">${nb(d.operations)}</td>
      <td><div class="mini-jauge"><span class="rail"><span style="width:${d.taux_automatisation}%"></span></span><b>${pct(d.taux_automatisation)}</b></div></td>
      <td class="num ${d.manquant ? "rouge" : ""}">${nb(d.manquant)}</td>
      <td class="num">${eur(d.montant_manquant)}</td>
      <td>${d.echeance_tva ? `${dateFr(d.echeance_tva)} <span class="attenue">· ${ech <= 0 ? "aujourd'hui" : `dans ${ech} j`}</span>` : "—"}</td>
      <td class="num">${d.a_une_relance ? `<a class="btn btn-fantome btn-petit" href="#/relances/${encodeURIComponent(d.code)}">Voir la relance</a>` : '<span class="attenue">Complet</span>'}</td>
    </tr>`;
  }).join("");

  return `
    <header class="entete-page">
      <div>
        <h1>Tableau de bord</h1>
        <p class="sous-titre">${esc(periode(a.periode))} · ${pluriel(a.volumes.dossiers, "dossier client", "dossiers clients")} · analysé le ${dateHeure(a.genere_le)}</p>
      </div>
      <div class="entete-actions">
        <a class="btn btn-secondaire" href="/api/export/tableau_suivi.csv" download>${ico("download")} Exporter le tableau de suivi</a>
      </div>
    </header>

    <section class="kpis">
      <article class="kpi kpi-accent">
        <p class="kpi-libelle">Traité sans intervention</p>
        <p class="kpi-valeur"><span data-compteur="${s.taux_automatisation}" data-format="pct">${pct(s.taux_automatisation)}</span></p>
        <div class="jauge"><span style="width:${s.taux_automatisation}%"></span></div>
        <p class="kpi-note">${nb(auto)} opérations sur ${nb(s.operations)} classées automatiquement</p>
      </article>
      <article class="kpi kpi-alerte">
        <p class="kpi-libelle">Pièces manquantes</p>
        <p class="kpi-valeur"><span data-compteur="${s.manquant}">${nb(s.manquant)}</span></p>
        <p class="kpi-note">${eur(s.montant_manquant)} de paiements à justifier</p>
        <a class="kpi-lien" href="#/operations/manquant">Voir la liste →</a>
      </article>
      <article class="kpi">
        <p class="kpi-libelle">Reste au comptable</p>
        <p class="kpi-valeur"><span data-compteur="${s.lignes_a_traiter}">${nb(s.lignes_a_traiter)}</span> <small>sur ${nb(s.operations)}</small></p>
        <p class="kpi-note">${esc(reste)}</p>
      </article>
      <article class="kpi kpi-ok">
        <p class="kpi-libelle">Relances prêtes</p>
        <p class="kpi-valeur"><span data-compteur="${a.relances.length}">${nb(a.relances.length)}</span></p>
        <p class="kpi-note">Un seul e-mail par client, toutes pièces regroupées</p>
        <a class="kpi-lien" href="#/relances">Voir les e-mails →</a>
      </article>
    </section>

    <section class="carte">
      <div class="carte-entete"><h2>Répartition du relevé</h2><p>Cliquez sur une catégorie pour voir les opérations concernées.</p></div>
      <div class="empile" role="img" aria-label="Répartition des opérations par statut">${segments}</div>
      <ul class="legende">${legende}</ul>
    </section>

    <div class="grille-2">
      <section class="carte">
        <div class="carte-entete">
          <h2>Pourquoi ${pluriel(s.hors_perimetre, "opération n'exige", "opérations n'exigent")} aucune pièce</h2>
          <p>Sur un relevé de PME, la majorité des lignes ne demande pas de facture. Chacune porte le nom de la règle appliquée.</p>
        </div>
        <ul class="barres">${barres}</ul>
      </section>
      <section class="carte">
        <div class="carte-entete">
          <h2>Cas difficiles résolus</h2>
          <p>Les situations qui piègent un rapprochement manuel ou un outil générique.</p>
        </div>
        ${faits}
      </section>
    </div>

    <section class="carte">
      <div class="carte-entete"><h2>Par dossier client</h2></div>
      <div class="table-defile">
        <table class="tableau">
          <thead><tr><th>Dossier</th><th class="num">Opérations</th><th>Automatisé</th><th class="num">Manquantes</th><th class="num">À justifier</th><th>Échéance TVA</th><th></th></tr></thead>
          <tbody>${lignesDossiers}</tbody>
        </table>
      </div>
    </section>`;
}

// -- vue : operations ------------------------------------------------------------------

function vueOperations(param) {
  const a = etat.analyse;
  if (!a) return "";
  if (param && (STATUTS[param] || param === "tous")) etat.filtres.statut = param;
  const compte = { tous: a.operations.length };
  for (const o of a.operations) compte[o.statut] = (compte[o.statut] || 0) + 1;

  const puces = ["tous", ...ORDRE_FILTRES].map((st) => `
    <button class="puce" data-action="statut" data-statut="${st}" aria-pressed="${etat.filtres.statut === st}">
      ${st === "tous" ? "Toutes" : esc(STATUTS[st].filtre)} <span class="n">${nb(compte[st] || 0)}</span>
    </button>`).join("");
  const options = a.par_dossier.map((d) =>
    `<option value="${esc(d.code)}" ${etat.filtres.dossier === d.code ? "selected" : ""}>${esc(d.raison_sociale)}</option>`).join("");
  const fleche = (col) => (etat.tri.col === col ? (etat.tri.sens > 0 ? " ↑" : " ↓") : "");

  return `
    <header class="entete-page">
      <div>
        <h1>Opérations bancaires</h1>
        <p class="sous-titre">Chaque ligne du relevé, sa décision et la règle qui l'a tranchée. Cliquez sur une ligne pour le détail.</p>
      </div>
      <div class="entete-actions">
        <a class="btn btn-secondaire" href="/api/export/tableau_suivi.csv" download>${ico("download")} Exporter</a>
      </div>
    </header>
    <section class="carte">
      <div class="filtres">
        <div class="puces-filtre" role="group" aria-label="Filtrer par statut">${puces}</div>
        <div class="champ-groupe">
          <select class="saisie" data-filtre="dossier" aria-label="Filtrer par dossier">
            <option value="tous">Tous les dossiers</option>${options}
          </select>
          <label class="recherche">${ico("recherche")}
            <input class="saisie" type="search" data-filtre="recherche" placeholder="Libellé, fournisseur, montant…" value="${esc(etat.filtres.recherche)}" aria-label="Rechercher">
          </label>
        </div>
      </div>
      <p class="compte-lignes" id="compte-ops"></p>
      <div class="table-defile">
        <table class="tableau">
          <thead><tr>
            <th><button data-action="trier" data-col="date">Date${fleche("date")}</button></th>
            <th>Dossier</th>
            <th>Libellé bancaire</th>
            <th class="num"><button data-action="trier" data-col="montant">Montant${fleche("montant")}</button></th>
            <th>Statut</th>
            <th>Décision</th>
          </tr></thead>
          <tbody id="corps-ops"></tbody>
        </table>
      </div>
    </section>`;
}

function operationsFiltrees() {
  const { statut, dossier, recherche } = etat.filtres;
  const q = recherche.trim().toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "");
  const qMontant = q.replace(/\s/g, "").replace(",", ".");
  let lignes = etat.analyse.operations.filter((o) => {
    if (statut !== "tous" && o.statut !== statut) return false;
    if (dossier !== "tous" && o.dossier !== dossier) return false;
    if (!q) return true;
    const texte = [o.libelle, o.reference, o.motif, ...o.pieces.map((p) => `${p.fournisseur} ${p.id}`)]
      .join(" ").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "");
    return texte.includes(q) || o.montant.includes(qMontant);
  });
  const { col, sens } = etat.tri;
  lignes = [...lignes].sort((x, y) => {
    const d = col === "montant" ? Number(x.montant) - Number(y.montant) : x.date.localeCompare(y.date);
    return d * sens || x.reference.localeCompare(y.reference);
  });
  return lignes;
}

// Deux lignes courtes par operation : ce qui a ete trouve, puis pourquoi.
function resumeDecision(o) {
  if (o.pieces.length) {
    const fournisseurs = o.pieces.map((p) => p.fournisseur).filter((v, i, t) => t.indexOf(v) === i);
    return [fournisseurs.join(", "), [o.pieces.map((p) => p.id).join(" + "), libelleRegle(o.regle)].join(" · ")];
  }
  if (o.statut === "manquant") return ["Aucune facture reçue", "À réclamer au client"];
  return [libelleRegle(o.regle), o.motif];
}

function majTableOperations() {
  const corps = document.getElementById("corps-ops");
  if (!corps) return;
  const lignes = operationsFiltrees();
  const MAX = 1000;
  corps.innerHTML = lignes.length ? lignes.slice(0, MAX).map((o) => {
    const signe = o.sens === "credit" ? "+" : "−";
    const [principal, detail] = resumeDecision(o);
    return `
      <tr data-action="ouvrir-op" data-ref="${esc(o.reference)}" tabindex="0">
        <td>${dateFr(o.date)}</td>
        <td><span class="code-dossier">${esc(o.dossier)}</span></td>
        <td class="libelle" title="${esc(o.libelle)}">${esc(o.libelle)}</td>
        <td class="num ${o.sens === "credit" ? "montant-credit" : ""}">${signe}${eur(o.montant, o.devise)}</td>
        <td>${badge(o.statut)}</td>
        <td class="decision-cellule" title="${esc(o.motif || detail)}"><strong>${esc(principal)}</strong><small>${esc(detail)}</small></td>
      </tr>`;
  }).join("") : '<tr><td colspan="6"><div class="vide"><strong>Aucune opération ne correspond</strong><span>Modifiez les filtres ou la recherche.</span></div></td></tr>';
  const total = etat.analyse.operations.length;
  document.getElementById("compte-ops").textContent =
    `${pluriel(lignes.length, "opération affichée", "opérations affichées")} sur ${nb(total)}` +
    (lignes.length > MAX ? ` (les ${nb(MAX)} premières)` : "");
}

// -- tiroir : detail d'une operation ----------------------------------------------------

function ouvrirOperation(reference) {
  const o = etat.analyse.operations.find((x) => x.reference === reference);
  if (!o) return;
  const signe = o.sens === "credit" ? "+" : "−";
  const decision = o.motif || (o.statut === "justifie"
    ? "Pièce rattachée automatiquement : le montant, la date et le fournisseur concordent."
    : "");
  const confiance = o.confiance > 0 && o.statut !== "hors_perimetre" ? `
    <div class="confiance">Confiance du rapprochement
      <span class="rail"><span style="width:${Math.round(o.confiance * 100)}%"></span></span>
      <b>${Math.round(o.confiance * 100)} %</b>
    </div>` : "";
  const pieces = o.pieces.length ? `
    <section class="section">
      <h3>${o.pieces.length > 1 ? `${o.pieces.length} pièces rattachées` : "Pièce rattachée"}</h3>
      ${o.pieces.map((p) => `
        <div class="piece">
          <span class="ico">${ico("fichier")}</span>
          <span><strong>${esc(p.fournisseur)}</strong><small>${esc(p.fichier || p.id)} · facture du ${dateFr(p.date)} · reçue par ${esc(p.canal)}</small></span>
          <b>${eur(p.montant, p.devise)}</b>
        </div>`).join("")}
    </section>` : "";
  const apprendre = o.statut === "manquant" ? `
    <details class="apprendre">
      <summary>Cette opération n'exige pas de facture ?</summary>
      <div class="apprendre-corps">
      <p>Apprenez-le une seule fois : les opérations de ce dossier dont le libellé contient ces mots ne seront plus réclamées au client.</p>
      <label class="champ">Mots du libellé
        <input class="saisie" id="regle-expr" value="${esc(o.suggestion || "")}" autocomplete="off">
      </label>
      <label class="champ">Justification <small>(affichée dans le tableau de suivi)</small>
        <input class="saisie" id="regle-motif" placeholder="Ex. : appel de fonds du syndic, justifié par l'appel annuel" autocomplete="off">
      </label>
      <div><button class="btn btn-primaire" data-action="apprendre" data-dossier="${esc(o.dossier)}">${ico("apprise")} Ne plus réclamer pour ce dossier</button></div>
      </div>
    </details>` : "";

  ouvrirCouche(`
    <div class="fond" data-action="fermer"></div>
    <aside class="tiroir" role="dialog" aria-modal="true" aria-labelledby="tiroir-titre">
      <div class="tiroir-entete">${badge(o.statut)}<button class="btn btn-icone" data-action="fermer" aria-label="Fermer">${ico("fermer")}</button></div>
      <div class="tiroir-corps">
        <div>
          <h2 class="tiroir-libelle" id="tiroir-titre">${esc(o.libelle)}</h2>
          <p class="tiroir-montant ${o.sens === "credit" ? "montant-credit" : ""}">${signe}${eur(o.montant, o.devise)}</p>
        </div>
        <dl class="details">
          <div><dt>Date d'opération</dt><dd>${dateFr(o.date)}</dd></div>
          <div><dt>Dossier</dt><dd>${esc(raisonSociale(o.dossier))}</dd></div>
          <div><dt>Référence</dt><dd>${esc(o.reference)}</dd></div>
          <div><dt>Sens</dt><dd>${o.sens === "credit" ? "Crédit (encaissement)" : "Débit (paiement)"}</dd></div>
        </dl>
        <section class="section">
          <h3>Décision</h3>
          <p class="decision">${esc(decision)}</p>
          <p class="compte-lignes">Règle appliquée : <strong>${esc(libelleRegle(o.regle))}</strong> <code>${esc(o.regle)}</code></p>
          ${confiance}
        </section>
        ${pieces}
        ${apprendre}
      </div>
    </aside>`);
}

async function apprendreRegle(bouton) {
  const expr = document.getElementById("regle-expr").value.trim();
  const motif = document.getElementById("regle-motif").value.trim();
  bouton.disabled = true;
  try {
    const rep = await api("/api/regles/ajouter", { dossier: bouton.dataset.dossier, motif_regex: expr, motif });
    etat.analyse = rep.analyse;
    etat.reveler = true;
    toast(rep.reclassees
      ? `Règle apprise : ${pluriel(rep.reclassees, "opération reclassée", "opérations reclassées")} « sans pièce nécessaire ».`
      : "Règle enregistrée. Elle s'appliquera aux prochains relevés.");
    await rendre();
  } catch (e) {
    toast(e.message, "erreur");
    bouton.disabled = false;
  }
}

// -- vue : relances ----------------------------------------------------------------------

function vueRelances(param) {
  const a = etat.analyse;
  if (!a) return "";
  const entete = `
    <header class="entete-page">
      <div>
        <h1>Relances à envoyer</h1>
        <p class="sous-titre">Brouillons préparés automatiquement : un seul e-mail par client, toutes les pièces manquantes regroupées.</p>
      </div>
    </header>`;
  if (!a.relances.length) {
    return `${entete}<section class="carte"><div class="vide"><span class="ico">${ico("check")}</span><strong>Aucune pièce manquante</strong><span>Tous les paiements sont justifiés : aucune relance à envoyer ce mois-ci.</span></div></section>`;
  }
  const choisie = a.relances.find((r) => r.dossier === param) || a.relances.find((r) => r.dossier === etat.relance) || a.relances[0];
  etat.relance = choisie.dossier;

  const liste = a.relances.map((r) => `
    <button data-action="choisir-relance" data-dossier="${esc(r.dossier)}" aria-current="${r.dossier === choisie.dossier}">
      <span class="haut"><strong>${esc(r.raison_sociale)}</strong><span class="montant">${eur(r.montant_total)}</span></span>
      <small>${pluriel(r.nombre_pieces, "pièce manquante", "pièces manquantes")} · ${esc(r.destinataire)}</small>
      <span><span class="etiquette ${esc(r.ton)}">${esc(TONS[r.ton] || r.ton)}</span></span>
    </button>`).join("");

  const mailto = `mailto:${encodeURIComponent(choisie.destinataire)}?subject=${encodeURIComponent(choisie.objet)}&body=${encodeURIComponent(choisie.corps)}`;
  const ech = choisie.echeance ? joursAvant(choisie.echeance) : null;
  return `${entete}
    <div class="relances">
      <nav class="relances-liste" aria-label="Relances par dossier">${liste}</nav>
      <article class="mail">
        <div class="mail-bandeau">${ico("info")} Brouillon : rien n'est envoyé sans relecture. Le texte reste modifiable dans votre messagerie.</div>
        <header class="mail-entete">
          <div class="mail-ligne"><span>À</span><strong>${esc(choisie.destinataire)}</strong></div>
          <div class="mail-ligne"><span>Objet</span><strong>${esc(choisie.objet)}</strong></div>
          <div class="mail-etiquettes">
            <span class="etiquette ${esc(choisie.ton)}">${esc(TONS[choisie.ton] || choisie.ton)}</span>
            ${choisie.echeance ? `<span class="etiquette">Échéance TVA : ${dateFr(choisie.echeance)}${ech !== null && ech >= 0 ? ` (dans ${ech} j)` : ""}</span>` : ""}
            <span class="etiquette">${pluriel(choisie.nombre_pieces, "pièce")} · ${eur(choisie.montant_total)}</span>
          </div>
        </header>
        <div class="mail-corps">${esc(choisie.corps)}</div>
        <footer class="mail-actions">
          <a class="btn btn-primaire" href="${mailto}">${ico("envoi")} Ouvrir dans ma messagerie</a>
          <button class="btn btn-secondaire" data-action="copier-relance">${ico("copie")} Copier le texte</button>
          <a class="btn btn-fantome" href="/api/export/relance/${encodeURIComponent(choisie.dossier)}.txt" download>${ico("download")} Télécharger (.txt)</a>
        </footer>
      </article>
    </div>`;
}

async function copierRelance() {
  const r = etat.analyse.relances.find((x) => x.dossier === etat.relance);
  if (!r) return;
  const texte = `Objet : ${r.objet}\n\n${r.corps}`;
  try {
    await navigator.clipboard.writeText(texte);
  } catch {
    const zone = document.createElement("textarea");
    zone.value = texte;
    document.body.appendChild(zone);
    zone.select();
    document.execCommand("copy");
    zone.remove();
  }
  toast("Texte de la relance copié dans le presse-papiers.");
}

// -- vue : regles du cabinet ---------------------------------------------------------------

function vueRegles() {
  const a = etat.analyse;
  if (!a) return "";
  const ref = a.referentiel;
  const apprises = ref.apprises.length ? `
    <div class="table-defile milieu">
      <table class="tableau">
        <thead><tr><th>Dossier</th><th>Mots du libellé</th><th>Justification</th><th class="num">Opérations écartées</th><th></th></tr></thead>
        <tbody>${ref.apprises.map((r) => `
          <tr>
            <td><span class="code-dossier">${esc(r.dossier)}</span></td>
            <td><code>${esc(r.motif_regex)}</code></td>
            <td>${esc(r.motif)}</td>
            <td class="num">${nb(r.operations)}</td>
            <td class="num"><button class="btn btn-danger btn-petit" data-action="oublier" data-dossier="${esc(r.dossier)}" data-regle="${esc(r.motif_regex)}">${ico("poubelle")} Retirer</button></td>
          </tr>`).join("")}
        </tbody>
      </table>
    </div>` : '<p class="compte-lignes">Aucune règle apprise pour l\'instant. Ajoutez-en une depuis une opération signalée à tort, ou ci-dessous.</p>';

  const options = a.par_dossier.map((d) => `<option value="${esc(d.code)}">${esc(d.raison_sociale)}</option>`).join("");
  const generales = ref.generales.map((r) => `
    <li><span><strong>${esc(libelleRegle(r.nom))}</strong><span>${esc(r.motif)}</span></span><span class="n">${pluriel(r.operations, "opération")}</span></li>`).join("");
  const alias = ref.alias.length ? `
    <table class="tableau">
      <thead><tr><th>Libellé bancaire</th><th>Fournisseur</th></tr></thead>
      <tbody>${ref.alias.map((x) => `<tr><td><code>${esc(x.alias)}</code></td><td>${esc(x.fournisseur)}</td></tr>`).join("")}</tbody>
    </table>` : '<p class="compte-lignes">Aucun alias.</p>';

  return `
    <header class="entete-page">
      <div>
        <h1>Règles du cabinet</h1>
        <p class="sous-titre">Ce qui décide qu'une opération n'exige aucune facture. Le premier niveau vaut pour tout dossier français ; le second est propre à vos clients et s'enrichit chaque mois.</p>
      </div>
    </header>

    <section class="carte carte-mise-en-avant">
      <div class="carte-entete">
        <h2>Règles apprises par le cabinet</h2>
        <p>Opérations récurrentes propres à un dossier : syndic, cotisations ordinales, redevances… Une règle se crée en un clic depuis une opération réclamée à tort.</p>
      </div>
      ${apprises}
      <form class="formulaire-regle" data-form="regle">
        <label class="champ">Dossier<select class="saisie" name="dossier">${options}</select></label>
        <label class="champ">Mots du libellé<input class="saisie" name="expr" placeholder="ex. : redevance occupation" required autocomplete="off"></label>
        <label class="champ">Justification<input class="saisie" name="motif" placeholder="ex. : redevance annuelle, justifiée par la convention" autocomplete="off"></label>
        <button class="btn btn-primaire" type="submit">Ajouter</button>
      </form>
    </section>

    <div class="grille-2">
      <section class="carte">
        <div class="carte-entete"><h2>Règles générales</h2><p>Valables pour tout dossier français. Nombre d'opérations concernées dans ce relevé.</p></div>
        <ul class="liste-regles">${generales}</ul>
      </section>
      <div class="pile">
        <section class="carte">
          <div class="carte-entete"><h2>Toujours exiger une facture</h2><p>Ces opérations ressemblent à des échéances de prêt, mais donnent lieu à une facture : elles ne sont jamais écartées.</p></div>
          <ul class="puces">${ref.contre_exemples.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>
        </section>
        <section class="carte">
          <div class="carte-entete"><h2>Alias fournisseurs</h2><p>Libellés bancaires abrégés rattachés au nom qui figure sur les factures.</p></div>
          <div class="table-defile">${alias}</div>
        </section>
      </div>
    </div>`;
}

async function ajouterRegleFormulaire(form) {
  const donnees = new FormData(form);
  const bouton = form.querySelector("button");
  bouton.disabled = true;
  try {
    const rep = await api("/api/regles/ajouter", {
      dossier: donnees.get("dossier"), motif_regex: donnees.get("expr"), motif: donnees.get("motif"),
    });
    etat.analyse = rep.analyse;
    etat.reveler = true;
    toast(rep.reclassees
      ? `Règle ajoutée : ${pluriel(rep.reclassees, "opération reclassée", "opérations reclassées")}.`
      : "Règle ajoutée. Aucune opération de ce relevé n'est concernée.");
    await rendre();
  } catch (e) {
    toast(e.message, "erreur");
    bouton.disabled = false;
  }
}

async function oublierRegle(bouton) {
  bouton.disabled = true;
  try {
    etat.analyse = await api("/api/regles/retirer", { dossier: bouton.dataset.dossier, motif_regex: bouton.dataset.regle });
    etat.reveler = true;
    toast("Règle retirée : les opérations concernées sont de nouveau réclamées.");
    await rendre();
  } catch (e) {
    toast(e.message, "erreur");
    bouton.disabled = false;
  }
}

// -- vue : qualite ----------------------------------------------------------------------------

function vueQualite() {
  const a = etat.analyse;
  if (!a) return "";
  const entete = `
    <header class="entete-page">
      <div>
        <h1>Qualité mesurée</h1>
        <p class="sous-titre">Comparaison de chaque décision avec la vérité terrain : la liste des pièces manquantes établie par un comptable.</p>
      </div>
    </header>`;
  const ev = a.evaluation;
  if (!ev) {
    return `${entete}
      <div class="avertissement info">${ico("info")}<div><strong>Pas de vérité terrain pour ces fichiers.</strong> Pour mesurer la qualité sur vos dossiers, importez aussi <code>verite_terrain.csv</code> : pour chaque opération, le statut attendu selon votre comptable (justifie, manquant, hors_perimetre, partiel).</div></div>`;
  }
  const avert = a.source === "demo"
    ? `<div class="avertissement">${ico("alerte")}<div><strong>Mesuré sur le jeu de démonstration</strong> (données fictives, conçues par l'équipe produit). Ce résultat prouve l'absence de défaut structurel, pas une exactitude sur vos dossiers : celle-ci se mesure pendant le pilote, sur un mois de votre relevé et la liste des pièces manquantes établie par votre comptable.</div></div>`
    : `<div class="avertissement info">${ico("info")}<div><strong>Mesuré sur vos fichiers importés</strong>, avec la vérité terrain fournie.</div></div>`;
  const okFa = ev.taux_fausses_alertes < 0.02;
  const okMr = ev.manques_rates === 0;
  const categories = ev.par_statut.map((c) => `
    <li>
      <span class="nom">${esc(STATUTS[c.statut]?.libelle || c.statut)}</span>
      <span class="rail"><span style="width:${(100 * c.justes / Math.max(c.total, 1)).toFixed(1)}%;background:var(--ok-vif)"></span></span>
      <span class="n">${nb(c.justes)}/${nb(c.total)}</span>
    </li>`).join("");
  const erreurs = ev.erreurs.length ? `
    <section class="carte">
      <div class="carte-entete"><h2>Décisions à corriger (${nb(ev.erreurs.length)})</h2><p>Chaque écart devient un test de non-régression une fois corrigé.</p></div>
      <div class="table-defile"><table class="tableau">
        <thead><tr><th>Dossier</th><th>Libellé</th><th>Attendu</th><th>Obtenu</th><th>Règle</th></tr></thead>
        <tbody>${ev.erreurs.map((e) => `
          <tr data-action="ouvrir-op" data-ref="${esc(e.reference)}" tabindex="0">
            <td><span class="code-dossier">${esc(e.dossier)}</span></td>
            <td class="libelle">${esc(e.libelle)}</td>
            <td>${badge(e.attendu)}</td><td>${badge(e.obtenu)}</td>
            <td class="attenue">${esc(libelleRegle(e.regle))}</td>
          </tr>`).join("")}</tbody>
      </table></div>
    </section>` : "";

  return `${entete}
    ${avert}
    <section class="kpis kpis-2">
      <article class="kpi kpi-grand ${okFa ? "ok kpi-ok" : "ko kpi-alerte"}">
        <p class="kpi-libelle">Fausses alertes</p>
        <p class="kpi-valeur">${pct(100 * ev.taux_fausses_alertes, 2)}</p>
        <p class="kpi-note">${nb(ev.fausses_alertes)} sur ${nb(ev.non_relancer)} opérations qui ne devaient pas être réclamées</p>
        <p class="cible">${ico(okFa ? "check" : "croix")} Cible : moins de 2 %</p>
        <p class="explication">Une pièce réclamée alors qu'elle n'est pas due. Trop de fausses alertes, et le cabinet cesse d'utiliser l'outil.</p>
      </article>
      <article class="kpi kpi-grand ${okMr ? "ok kpi-ok" : "ko kpi-alerte"}">
        <p class="kpi-libelle">Pièces manquantes ratées</p>
        <p class="kpi-valeur">${pct(100 * ev.taux_manques_rates, 2)}</p>
        <p class="kpi-note">${nb(ev.manques_rates)} sur ${nb(ev.a_relancer)} pièces réellement manquantes</p>
        <p class="cible">${ico(okMr ? "check" : "croix")} Cible : 0 %</p>
        <p class="explication">Une pièce manquante non signalée : la déclaration partirait incomplète. C'est l'erreur qui ne doit jamais arriver.</p>
      </article>
    </section>
    <section class="carte">
      <div class="carte-entete">
        <h2>Décisions correctes : ${nb(ev.justes)} sur ${nb(ev.total)} (${pct(100 * ev.taux_correct)})</h2>
        <p>Par catégorie attendue.${ev.pieces_controlees ? ` Pièces rattachées : ${nb(ev.pieces_controlees - ev.pieces_erronees)} sur ${nb(ev.pieces_controlees)} conformes à la vérité terrain (la bonne facture sur le bon paiement).` : ""}</p>
      </div>
      <ul class="barres">${categories}</ul>
    </section>
    ${erreurs}`;
}

// -- import de fichiers ----------------------------------------------------------------------

function ouvrirImport() {
  etat.fichiers = {};
  const depot = (f) => `
    <div class="depot" data-cle="${f.cle}">
      <span class="ico">${ico("fichier")}</span>
      <span>
        <strong>${esc(f.titre)}</strong>
        <small>${esc(f.nom)} · ${f.obligatoire ? "obligatoire" : esc(f.aide || "facultatif")}</small>
        <span class="nom-fichier" id="nom-${f.cle}"></span>
      </span>
      <input type="file" accept=".csv,text/csv" data-fichier="${f.cle}" aria-label="${esc(f.titre)}">
      <a class="lien-modele" href="/modeles/${f.nom}" download>Modèle</a>
    </div>`;
  ouvrirCouche(`
    <div class="fond" data-action="fermer"></div>
    <div class="modale" role="dialog" aria-modal="true" aria-labelledby="modale-titre">
      <div class="modale-entete">
        <div>
          <h2 id="modale-titre">Importer vos fichiers</h2>
          <p class="sous-titre">Format CSV, séparateur virgule. Glissez les fichiers ou cliquez pour les choisir ; chaque modèle montre les colonnes attendues.</p>
        </div>
        <button class="btn btn-icone" data-action="fermer" aria-label="Fermer">${ico("fermer")}</button>
      </div>
      <div class="depots">
        ${FICHIERS_IMPORT.filter((f) => f.obligatoire).map(depot).join("")}
        <p class="sous-titre-depots">Facultatifs</p>
        ${FICHIERS_IMPORT.filter((f) => !f.obligatoire).map(depot).join("")}
      </div>
      <p class="erreur" id="erreur-import" hidden></p>
      <div class="avertissement info">${ico("cadenas")}<div>Les fichiers restent sur ce poste : ils sont lus par le serveur local, sans envoi sur Internet.</div></div>
      <div class="modale-pied">
        <button class="btn btn-fantome" data-action="revenir-demo">${ico("remise")} Données de démonstration</button>
        <span class="espace"></span>
        <button class="btn btn-secondaire" data-action="fermer">Annuler</button>
        <button class="btn btn-primaire" data-action="valider-import">${ico("play")} Importer et analyser</button>
      </div>
    </div>`);
}

async function lireFichier(fichier) {
  // Les exports Excel francais sont souvent en Windows-1252 : on essaie
  // l'UTF-8 strict, puis on retombe sur Windows-1252 sans perdre les accents.
  const octets = await fichier.arrayBuffer();
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(octets);
  } catch {
    return new TextDecoder("windows-1252").decode(octets);
  }
}

async function choisirFichier(cle, fichier) {
  if (!fichier) return;
  etat.fichiers[cle] = await lireFichier(fichier);
  const zone = document.querySelector(`.depot[data-cle="${cle}"]`);
  zone?.classList.add("rempli");
  const nom = document.getElementById(`nom-${cle}`);
  if (nom) nom.textContent = `${fichier.name} · ${Math.max(1, Math.round(fichier.size / 1024))} Ko`;
}

async function validerImport(bouton) {
  const erreur = document.getElementById("erreur-import");
  erreur.hidden = true;
  bouton.disabled = true;
  try {
    etat.analyse = await api("/api/importer", etat.fichiers);
    etat.reveler = true;
    toast(`Fichiers importés : ${pluriel(etat.analyse.volumes.operations, "opération")} analysées.`);
    fermerCouche();
    location.hash = "#/tableau";
    await rendre();
  } catch (e) {
    erreur.textContent = e.message;
    erreur.hidden = false;
    bouton.disabled = false;
  }
}

async function revenirDemo() {
  try {
    etat.analyse = await api("/api/demo", {});
    etat.reveler = true;
    etat.filtres = { statut: "tous", dossier: "tous", recherche: "" };
    toast("Données de démonstration rechargées.");
    fermerCouche();
    await rendre();
  } catch (e) {
    toast(e.message, "erreur");
  }
}

// -- couche (tiroir ou modale) -------------------------------------------------------------------

let focusAvant = null;
let fermeture = null;
function ouvrirCouche(html) {
  clearTimeout(fermeture);
  focusAvant = document.activeElement;
  const couche = document.getElementById("couche");
  couche.classList.remove("sortie");
  couche.innerHTML = html;
  const cible = couche.querySelector("input:not([type=file]), button");
  cible?.focus();
}
function fermerCouche() {
  const couche = document.getElementById("couche");
  if (!couche.innerHTML || couche.classList.contains("sortie")) return;
  const finir = () => {
    couche.classList.remove("sortie");
    couche.innerHTML = "";
    if (focusAvant?.isConnected) focusAvant.focus();
  };
  if (moinsDeMouvement()) return finir();
  // L'etat final ne depend pas de la fin de l'animation : minuterie fixe.
  couche.classList.add("sortie");
  fermeture = setTimeout(finir, 190);
}

// -- evenements ----------------------------------------------------------------------------------

document.addEventListener("click", async (ev) => {
  const choix = ev.target.closest("[data-theme-choix]");
  if (choix) return appliquerTheme(choix.dataset.themeChoix);
  const el = ev.target.closest("[data-action]");
  if (!el) return;
  const action = el.dataset.action;
  switch (action) {
    case "lancer": return lancer(el);
    case "importer": return ouvrirImport();
    case "fermer": return fermerCouche();
    case "valider-import": return validerImport(el);
    case "revenir-demo": return revenirDemo();
    case "ouvrir-op": return ouvrirOperation(el.dataset.ref);
    case "apprendre": return apprendreRegle(el);
    case "oublier": return oublierRegle(el);
    case "copier-relance": return copierRelance();
    case "filtrer":
      etat.filtres.statut = el.dataset.statut;
      location.hash = `#/operations/${el.dataset.statut}`;
      return;
    case "statut":
      etat.filtres.statut = el.dataset.statut;
      document.querySelectorAll(".puce").forEach((p) => p.setAttribute("aria-pressed", p.dataset.statut === etat.filtres.statut));
      history.replaceState(null, "", `#/operations/${etat.filtres.statut}`);
      majTableOperations();
      return fondu(document.getElementById("corps-ops"), 0);
    case "trier": {
      const col = el.dataset.col;
      etat.tri = { col, sens: etat.tri.col === col ? -etat.tri.sens : col === "montant" ? -1 : 1 };
      document.querySelectorAll("[data-action=trier]").forEach((b) => {
        const base = b.dataset.col === "date" ? "Date" : "Montant";
        b.textContent = base + (b.dataset.col === etat.tri.col ? (etat.tri.sens > 0 ? " ↑" : " ↓") : "");
      });
      majTableOperations();
      return fondu(document.getElementById("corps-ops"), 0);
    }
    case "choisir-relance":
      etat.relance = el.dataset.dossier;
      history.replaceState(null, "", `#/relances/${encodeURIComponent(el.dataset.dossier)}`);
      document.getElementById("vue").innerHTML = vueRelances(el.dataset.dossier);
      return fondu(document.querySelector(".mail"));
  }
});

document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") fermerCouche();
  if (ev.key === "Enter" && ev.target.matches("tr[data-action]")) ev.target.click();
});

document.addEventListener("input", (ev) => {
  const filtre = ev.target.dataset?.filtre;
  if (filtre) {
    etat.filtres[filtre] = ev.target.value;
    majTableOperations();
  }
});

document.addEventListener("change", (ev) => {
  if (ev.target.matches("select[data-filtre]")) fondu(document.getElementById("corps-ops"), 0);
  if (ev.target.matches("input[type=file][data-fichier]")) {
    choisirFichier(ev.target.dataset.fichier, ev.target.files[0]);
  }
});

document.addEventListener("submit", (ev) => {
  if (ev.target.dataset.form === "regle") {
    ev.preventDefault();
    ajouterRegleFormulaire(ev.target);
  }
});

for (const type of ["dragenter", "dragover"]) {
  document.addEventListener(type, (ev) => {
    const zone = ev.target.closest?.(".depot");
    if (!zone) return;
    ev.preventDefault();
    zone.classList.add("survol");
  });
}
document.addEventListener("dragleave", (ev) => ev.target.closest?.(".depot")?.classList.remove("survol"));
document.addEventListener("drop", (ev) => {
  const zone = ev.target.closest?.(".depot");
  if (!zone) return;
  ev.preventDefault();
  zone.classList.remove("survol");
  choisirFichier(zone.dataset.cle, ev.dataTransfer.files[0]);
});

window.addEventListener("hashchange", rendre);

(async function demarrer() {
  majBoutonsTheme();
  await charger();
  rendre();
})();
