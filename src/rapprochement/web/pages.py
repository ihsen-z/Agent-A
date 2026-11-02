"""Rendu des ecrans (fonctions pures : donnees -> `Html`).

Aucune fonction ici ne lit ni n'ecrit le disque. Toute valeur passe par
`gabarits.gabarit` / `gabarits.echapper` : aucun texte n'est interpole autrement.
Aucun JavaScript, aucun style en ligne (la CSP les interdit) : les classes CSS
renvoient a `static/style.css`. Un etat est toujours ecrit en toutes lettres, la
couleur ne fait que le doubler.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from ..audit import EntreeAudit, ResultatVerification
from ..cycle import LigneSuivi
from ..modeles import (
    ETATS_TERMINAUX,
    Brouillon,
    DecisionRoutage,
    Dossier,
    EtatPiece,
    MotifNonRoute,
    PieceAttendue,
    StatutBrouillon,
    StatutRoutage,
)
from .comptes import Utilisateur
from .gabarits import Html, gabarit, joindre

FUSEAU = ZoneInfo("Europe/Paris")
NOM_APPLICATION = "Pièces justificatives"
BANDEAU_MODE_TEST = "MODE TEST : DONNEES FICTIVES"

LIBELLES_ETAT = {
    EtatPiece.ATTENDUE: "Attendue",
    EtatPiece.DEMANDEE: "Demandée",
    EtatPiece.PROMISE: "Promise",
    EtatPiece.RECUE: "Reçue",
    EtatPiece.VALIDEE: "Validée",
    EtatPiece.ESCALADEE: "Escaladée",
    EtatPiece.HORS_PERIMETRE: "Hors périmètre",
    EtatPiece.CLOSE_SANS_SUITE: "Close sans suite",
}
LIBELLES_BROUILLON = {
    StatutBrouillon.BROUILLON: "À relire",
    StatutBrouillon.VALIDEE: "Validé, à envoyer",
    StatutBrouillon.EN_COURS: "Envoi incertain",
    StatutBrouillon.ENVOYEE: "Envoyé",
    StatutBrouillon.REJETEE: "Rejeté",
}
LIBELLES_MOTIF = {
    MotifNonRoute.DOSSIER_INCONNU: "Expéditeur non rattaché à un dossier",
    MotifNonRoute.DOSSIER_AMBIGU: "Expéditeur commun à plusieurs dossiers",
    MotifNonRoute.PERIODE_AMBIGUE: "Période ambiguë",
    MotifNonRoute.MONTANT_INCONNU: "Montant illisible ou multiple",
    MotifNonRoute.AUCUN_CANDIDAT: "Aucune opération correspondante",
    MotifNonRoute.CANDIDATS_MULTIPLES: "Plusieurs opérations possibles",
    MotifNonRoute.SANS_PIECE_JOINTE: "Message sans pièce jointe",
}
MESSAGES_FAIT = {
    "valide": "Brouillon validé. Il peut maintenant être envoyé.",
    "rejete": "Brouillon rejeté.",
    "envoye": "Relance envoyée : le message a été déposé dans outbox/.",
    "tranche_parti": "Envoi confirmé comme parti.",
    "tranche_non_parti": "Envoi déclaré non parti : le brouillon est de nouveau validé et peut être renvoyé.",
    "rattache": "Rattachement confirmé : la pièce est reçue et attend son contrôle.",
    "controle": "Contrôle enregistré.",
    "promesse": "Promesse enregistrée.",
    "bloque": "Pièce bloquée : elle ne sera plus relancée.",
    "debloque": "Pièce débloquée.",
    "arbitre": "Pièce classée sans suite.",
}


# ---------------------------------------------------------------------------
# Petits elements
# ---------------------------------------------------------------------------


def date_fr(d: dt.date | None) -> str:
    if d is None:
        return "—"
    if isinstance(d, dt.datetime):
        local = d.astimezone(FUSEAU) if d.utcoffset() is not None else d
        return local.strftime("%d/%m/%Y %H:%M")
    return d.strftime("%d/%m/%Y")


_CHIFFRES_FR = str.maketrans({",": " ", ".": ","})


def montant(m: Decimal, devise: str = "EUR") -> Html:
    try:
        texte = f"{m:,.2f}".translate(_CHIFFRES_FR)
    except (TypeError, ValueError):
        texte = str(m)
    return gabarit('<span class="montant">{t}&nbsp;{d}</span>', t=texte, d=devise)


def badge_etat(etat: EtatPiece, bloquee: bool = False) -> Html:
    badge = gabarit('<span class="etat etat-{c}">{t}</span>', c=etat.value, t=LIBELLES_ETAT.get(etat, etat.value))
    if bloquee:
        badge = joindre([badge, Html(" "), Html('<span class="etat etat-bloquee">Bloquée</span>')])
    return badge


def badge_brouillon(statut: StatutBrouillon) -> Html:
    return gabarit('<span class="etat statut-{c}">{t}</span>', c=statut.value,
                   t=LIBELLES_BROUILLON.get(statut, statut.value))


def lien(href: str, texte: Any, classe: str = "") -> Html:
    if classe:
        return gabarit('<a class="{c}" href="{h}">{t}</a>', c=classe, h=href, t=texte)
    return gabarit('<a href="{h}">{t}</a>', h=href, t=texte)


def champ_cache(nom: str, valeur: Any) -> Html:
    return gabarit('<input type="hidden" name="{n}" value="{v}">', n=nom, v=valeur)


def formulaire(action: str, csrf: str, contenu: Html | str, bouton: str, *,
               classe_bouton: str = "bouton", classe: str = "formulaire") -> Html:
    return gabarit(
        '<form class="{cl}" method="post" action="{a}">{csrf}{contenu}'
        '<button type="submit" class="{cb}">{b}</button></form>',
        cl=classe, a=action, csrf=champ_cache("csrf", csrf), contenu=contenu,
        cb=classe_bouton, b=bouton,
    )


def champ_texte(nom: str, etiquette: str, ident: str, *, requis: bool = False, valeur: str = "",
                type_: str = "text", max_: int = 500, autocomplete: str = "off") -> Html:
    return gabarit(
        '<p class="champ"><label for="{i}">{e}</label>'
        '<input id="{i}" name="{n}" type="{t}" value="{v}" maxlength="{m}" autocomplete="{ac}"{r}></p>',
        i=ident, e=etiquette, n=nom, t=type_, v=valeur, m=max_, ac=autocomplete,
        r=Html(" required") if requis else Html(""),
    )


def tableau(legende: str, entetes: Sequence[str | tuple[str, str]], lignes: Iterable[Html], *,
            vide: str = "Aucun élément.") -> Html:
    lignes = list(lignes)
    if not lignes:
        return gabarit('<p class="vide">{v}</p>', v=vide)
    cellules = []
    for e in entetes:
        texte, classe = (e, "") if isinstance(e, str) else e
        cellules.append(gabarit('<th scope="col" class="{c}">{t}</th>', c=classe, t=texte))
    return gabarit(
        '<div class="tableau" role="region" aria-label="{l}" tabindex="0"><table>'
        '<caption>{l}</caption><thead><tr>{th}</tr></thead><tbody>{tb}</tbody></table></div>',
        l=legende, th=joindre(cellules), tb=joindre(lignes),
    )


def ligne(*cellules: Any, classes: Sequence[str] = ()) -> Html:
    tds = []
    for i, c in enumerate(cellules):
        classe = classes[i] if i < len(classes) else ""
        tds.append(gabarit('<td class="{c}">{v}</td>', c=classe, v=c))
    return gabarit("<tr>{t}</tr>", t=joindre(tds))


def avis(texte: Any, genre: str = "ok") -> Html:
    role = "alert" if genre == "erreur" else "status"
    return gabarit('<div class="avis avis-{g}" role="{r}"><p>{t}</p></div>', g=genre, r=role, t=texte)


def carte(titre: str, contenu: Html, *, lien_href: str = "", lien_texte: str = "") -> Html:
    pied = gabarit('<p class="carte-lien">{l}</p>', l=lien(lien_href, lien_texte)) if lien_href else Html("")
    return gabarit('<section class="carte"><h2>{t}</h2>{c}{p}</section>', t=titre, c=contenu, p=pied)


def definitions(paires: Iterable[tuple[str, Any]]) -> Html:
    return gabarit('<dl class="fiche">{c}</dl>', c=joindre(
        gabarit("<div><dt>{k}</dt><dd>{v}</dd></div>", k=k, v=v) for k, v in paires
    ))


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Contexte:
    """Ce dont toute page a besoin pour son cadre."""

    utilisateur: Utilisateur | None
    csrf: str
    mode_test: bool
    actif: str = ""


_NAVIGATION = (
    ("accueil", "/", "Accueil", False),
    ("brouillons", "/brouillons", "Brouillons", False),
    ("non-rattaches", "/non-rattaches", "Non rattachés", False),
    ("pieces", "/pieces", "Pièces", False),
    ("escalades", "/escalades", "Escalades", False),
    ("audit", "/audit", "Audit", True),
)


def document(ctx: Contexte, titre: str, contenu: Html, *, message: Html | None = None) -> Html:
    bandeau = (gabarit('<div class="bandeau-test" role="note"><p>{t}</p></div>', t=BANDEAU_MODE_TEST)
               if ctx.mode_test else Html(""))
    if ctx.utilisateur is not None:
        u = ctx.utilisateur
        liens = []
        for cle, href, texte, reserve in _NAVIGATION:
            if reserve and not u.est_responsable:
                continue
            courant = Html(' aria-current="page"') if cle == ctx.actif else Html("")
            liens.append(gabarit('<li><a href="{h}"{c}>{t}</a></li>', h=href, c=courant, t=texte))
        navigation = gabarit('<nav class="navigation" aria-label="Navigation principale"><ul>{l}</ul></nav>',
                             l=joindre(liens))
        compte = gabarit(
            '<div class="compte"><p><span class="compte-nom">{n}</span> '
            '<span class="compte-role">{r}</span></p>{f}</div>',
            n=u.nom, r=u.role,
            f=formulaire("/deconnexion", ctx.csrf, Html(""), "Se déconnecter", classe_bouton="bouton bouton-discret"),
        )
    else:
        navigation, compte = Html(""), Html("")
    return gabarit(
        "<!doctype html>\n"
        '<html lang="fr"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>{titre} · {app}</title>"
        '<link rel="stylesheet" href="/style.css"></head>'
        '<body><a class="evitement" href="#contenu">Aller au contenu</a>{bandeau}'
        '<header class="entete"><div class="entete-int">'
        '<p class="marque"><a href="/">{app}</a></p>{nav}{compte}</div></header>'
        '<main id="contenu" class="contenu"><h1>{titre}</h1>{message}{contenu}</main>'
        '<footer class="pied"><p>Aucun e-mail ne part sans la validation d’une personne nommée. '
        "Les envois sont déposés dans le dossier outbox/ de l’instance.</p></footer>"
        "</body></html>\n",
        titre=titre, app=NOM_APPLICATION, bandeau=bandeau, nav=navigation, compte=compte,
        message=message if message is not None else Html(""), contenu=contenu,
    )


def message_fait(code: str) -> Html | None:
    texte = MESSAGES_FAIT.get(code)
    return avis(texte) if texte else None


def page_erreur(ctx: Contexte, titre: str, message: Any, *, retour: str | None = None) -> Html:
    contenu = avis(message, "erreur")
    if retour:
        contenu = joindre([contenu, gabarit('<p>{l}</p>', l=lien(retour, "Revenir à la page précédente"))])
    return document(ctx, titre, contenu)


# ---------------------------------------------------------------------------
# Connexion
# ---------------------------------------------------------------------------


def page_connexion(ctx: Contexte, *, erreur: str = "", identifiant: str = "") -> Html:
    champs = joindre([
        champ_texte("identifiant", "Identifiant", "identifiant", requis=True, valeur=identifiant,
                    max_=64, autocomplete="username"),
        champ_texte("mot_de_passe", "Mot de passe", "mot_de_passe", requis=True, type_="password",
                    max_=1024, autocomplete="current-password"),
    ])
    contenu = joindre([
        avis(erreur, "erreur") if erreur else Html(""),
        gabarit('<section class="carte carte-etroite">{f}</section>',
                f=formulaire("/connexion", ctx.csrf, champs, "Se connecter")),
    ])
    return document(ctx, "Connexion", contenu)


def page_deconnexion(ctx: Contexte) -> Html:
    return document(ctx, "Déconnexion", joindre([
        gabarit("<p>{t}</p>", t="Terminer la session sur ce poste ?"),
        formulaire("/deconnexion", ctx.csrf, Html(""), "Se déconnecter"),
    ]))


# ---------------------------------------------------------------------------
# Accueil
# ---------------------------------------------------------------------------


def page_accueil(ctx: Contexte, *, pieces: Sequence[PieceAttendue], brouillons: Sequence[Brouillon],
                 file: Sequence[DecisionRoutage], verification: ResultatVerification) -> Html:
    compte = {e: 0 for e in EtatPiece}
    for p in pieces:
        compte[p.etat] += 1
    bloquees = sum(1 for p in pieces if p.bloquee and p.etat not in ETATS_TERMINAUX)
    lignes_etats = [
        ligne(badge_etat(e), lien(f"/pieces?etat={e.value}", str(n)), classes=("", "nombre"))
        for e, n in compte.items()
    ]
    lignes_etats.append(ligne(Html('<span class="etat etat-bloquee">Bloquées</span>'), str(bloquees),
                              classes=("", "nombre")))
    carte_pieces = carte("Pièces par état", tableau("Pièces par état", ["État", ("Nombre", "nombre")], lignes_etats),
                         lien_href="/pieces", lien_texte="Toutes les pièces")

    a_relire = [b for b in brouillons if b.statut is StatutBrouillon.BROUILLON]
    a_envoyer = [b for b in brouillons if b.statut is StatutBrouillon.VALIDEE]
    incertains = [b for b in brouillons if b.statut is StatutBrouillon.EN_COURS]
    liste = joindre(
        gabarit("<li>{l} <span class=\"secondaire\">{d}</span></li>",
                l=lien(f"/brouillons/{b.id_relance}", b.objet or b.id_relance), d=b.destinataire)
        for b in a_relire[:10]
    )
    carte_brouillons = carte("Brouillons à relire", joindre([
        gabarit('<p class="chiffre">{n}</p><p class="secondaire">{v} validé(s) à envoyer, {i} envoi(s) incertain(s)</p>',
                n=len(a_relire), v=len(a_envoyer), i=len(incertains)),
        gabarit('<ul class="liste">{l}</ul>', l=liste) if a_relire else Html(""),
    ]), lien_href="/brouillons?statut=brouillon", lien_texte="Relire les brouillons")

    escaladees = compte[EtatPiece.ESCALADEE]
    carte_escalades = carte("Escalades", gabarit(
        '<p class="chiffre">{n}</p><p class="secondaire">pièce(s) escaladée(s) au responsable</p>', n=escaladees),
        lien_href="/escalades", lien_texte="Voir les escalades")
    carte_file = carte("Messages non rattachés", gabarit(
        '<p class="chiffre">{n}</p><p class="secondaire">pièce(s) reçue(s) à rattacher par une personne</p>',
        n=len(file)), lien_href="/non-rattaches", lien_texte="Traiter la file")
    etat_journal = (gabarit('<p><span class="etat etat-ok">Intègre</span> {n} entrée(s) vérifiée(s).</p>',
                            n=verification.nb_entrees)
                    if verification.ok else
                    gabarit('<p><span class="etat etat-alerte">Altéré</span> {r}</p>'
                            '<p class="secondaire">Prévenir le responsable : aucune écriture n’est possible '
                            'tant que le journal n’est pas sain.</p>', r=verification.raison))
    carte_journal = carte("Journal d’audit", etat_journal,
                          lien_href="/audit" if ctx.utilisateur and ctx.utilisateur.est_responsable else "",
                          lien_texte="Ouvrir le journal")
    return gabarit('<div class="grille">{a}{b}{c}{d}{e}</div>', a=carte_brouillons, b=carte_file,
                   c=carte_escalades, d=carte_pieces, e=carte_journal)


# ---------------------------------------------------------------------------
# Brouillons
# ---------------------------------------------------------------------------


def filtre_select(nom: str, etiquette: str, options: Iterable[tuple[str, str]], choisi: str) -> Html:
    opts = [gabarit('<option value="">{t}</option>', t="Tous")]
    for valeur, texte in options:
        sel = Html(" selected") if valeur == choisi else Html("")
        opts.append(gabarit('<option value="{v}"{s}>{t}</option>', v=valeur, s=sel, t=texte))
    return gabarit('<p class="champ"><label for="f-{n}">{e}</label><select id="f-{n}" name="{n}">{o}</select></p>',
                   n=nom, e=etiquette, o=joindre(opts))


def page_brouillons(brouillons: Sequence[Brouillon], statut: str) -> Html:
    filtre = gabarit(
        '<form class="filtres" method="get" action="/brouillons">{s}'
        '<button type="submit" class="bouton bouton-discret">Filtrer</button></form>',
        s=filtre_select("statut", "Statut", ((s.value, LIBELLES_BROUILLON[s]) for s in StatutBrouillon), statut),
    )
    lignes = [
        ligne(date_fr(b.cree_le), b.destinataire, lien(f"/brouillons/{b.id_relance}", b.objet or b.id_relance),
              ", ".join(b.dossiers), str(b.niveau), badge_brouillon(b.statut),
              classes=("date", "", "", "", "nombre", ""))
        for b in brouillons
    ]
    return joindre([filtre, tableau(
        "Brouillons de relance",
        ["Créé le", "Destinataire", "Objet", "Dossiers", ("Niveau", "nombre"), "Statut"],
        lignes, vide="Aucun brouillon pour ce filtre.")])


def _circuit(b: Brouillon, dossiers: Mapping[str, Dossier]) -> str:
    morceaux = []
    for code in b.dossiers:
        d = dossiers.get(code)
        morceaux.append(f"{d.circuit} ({code}, {d.raison_sociale})" if d else f"inconnu ({code})")
    return " ; ".join(morceaux)


def _lignes_pieces_reclamees(b: Brouillon, pieces: Mapping[tuple[str, str], PieceAttendue]) -> list[Html]:
    lignes = []
    for ref in b.references:
        trouvees = [pieces[(d, ref)] for d in b.dossiers if (d, ref) in pieces]
        if len(trouvees) != 1:
            lignes.append(ligne("—", ref, "—", "pièce introuvable ou ambiguë", "—", "—",
                                classes=("", "", "", "", "nombre", "")))
            continue
        p = trouvees[0]
        lignes.append(ligne(p.dossier, p.reference, date_fr(p.date_operation), p.libelle,
                            montant(p.montant, p.devise), badge_etat(p.etat, p.bloquee),
                            classes=("code", "code", "date", "", "nombre", "")))
    return lignes


def page_brouillon(ctx: Contexte, b: Brouillon, *, pieces: Mapping[tuple[str, str], PieceAttendue],
                   dossiers: Mapping[str, Dossier]) -> Html:
    fiche = definitions([
        ("Statut", badge_brouillon(b.statut)),
        ("Destinataire", b.destinataire),
        ("Circuit", _circuit(b, dossiers)),
        ("Dossiers", ", ".join(b.dossiers)),
        ("Niveau", "demande initiale" if b.niveau == 1 else f"relance n° {b.niveau}"),
        ("Créé le", date_fr(b.cree_le)),
        ("Validé par", f"{b.valide_par} le {date_fr(b.valide_le)}" if b.valide_par else "—"),
        ("Envoyé le", date_fr(b.envoye_le)),
        ("Identifiant", b.id_relance),
    ])
    message = gabarit(
        '<section class="carte"><h2>Message</h2><p class="objet"><span class="secondaire">Objet :</span> {o}</p>'
        '<pre class="corps">{c}</pre></section>', o=b.objet, c=b.corps)
    reclamees = carte("Pièces réclamées", tableau(
        "Pièces réclamées par ce message",
        ["Dossier", "Référence", "Date d’opération", "Libellé bancaire", ("Montant", "nombre"), "État"],
        _lignes_pieces_reclamees(b, pieces)))
    return joindre([
        gabarit('<p class="fil">{l}</p>', l=lien("/brouillons", "← Tous les brouillons")),
        gabarit('<div class="deux-colonnes"><section class="carte"><h2>Fiche</h2>{f}</section>{a}</div>',
                f=fiche, a=_actions_brouillon(ctx, b)),
        message, reclamees,
    ])


def _actions_brouillon(ctx: Contexte, b: Brouillon) -> Html:
    base = f"/brouillons/{b.id_relance}"
    actions: list[Html] = []
    rejet = formulaire(
        f"{base}/rejeter", ctx.csrf,
        champ_texte("motif", "Motif du rejet", f"motif-rejet-{b.id_relance}", requis=True),
        "Rejeter", classe_bouton="bouton bouton-danger")
    if b.statut is StatutBrouillon.BROUILLON:
        actions.append(gabarit('<p>{t}</p>', t="Relisez le message et les pièces réclamées avant de valider."))
        actions.append(formulaire(f"{base}/valider", ctx.csrf, Html(""), "Valider ce brouillon"))
        actions.append(rejet)
    elif b.statut is StatutBrouillon.VALIDEE:
        actions.append(gabarit('<p>{l}</p>', l=lien(f"{base}/envoyer", "Envoyer…", "bouton")))
        actions.append(rejet)
    elif b.statut is StatutBrouillon.EN_COURS:
        actions.append(gabarit(
            "<p>{t}</p>", t="L’envoi a commencé mais son résultat est inconnu. Vérifiez la présence du "
            "message dans outbox/ (ou dans les envoyés) avant de trancher. Il ne sera jamais renvoyé seul."))
        if ctx.utilisateur is not None and ctx.utilisateur.est_responsable:
            actions.append(formulaire(f"{base}/trancher", ctx.csrf, champ_cache("parti", "oui"),
                                      "Le message est bien parti"))
            actions.append(formulaire(f"{base}/trancher", ctx.csrf, champ_cache("parti", "non"),
                                      "Le message n’est pas parti", classe_bouton="bouton bouton-discret"))
        else:
            actions.append(gabarit('<p class="secondaire">{t}</p>', t="À trancher par un responsable."))
    else:
        actions.append(gabarit('<p class="secondaire">{t}</p>', t="Aucune action possible : ce brouillon est clos."))
    return gabarit('<section class="carte"><h2>Actions</h2>{a}</section>', a=joindre(actions))


def page_confirmation_envoi(ctx: Contexte, b: Brouillon) -> Html:
    base = f"/brouillons/{b.id_relance}"
    if b.statut is not StatutBrouillon.VALIDEE:
        return joindre([
            avis(f"Ce brouillon n’est pas envoyable : statut « {LIBELLES_BROUILLON[b.statut]} ». "
                 "Seul un brouillon validé par une personne nommée peut partir.", "erreur"),
            gabarit("<p>{l}</p>", l=lien(base, "Revenir au brouillon")),
        ])
    return joindre([
        definitions([("Destinataire", b.destinataire), ("Objet", b.objet), ("Validé par", b.valide_par),
                     ("Pièces réclamées", ", ".join(b.references))]),
        gabarit("<p>{t}</p>", t="Confirmez-vous l’envoi de ce message ? Il sera déposé dans outbox/ "
                "(aucun envoi réel) et ne pourra plus être renvoyé."),
        gabarit('<div class="boutons">{f} {l}</div>',
                f=formulaire(f"{base}/envoyer", ctx.csrf, champ_cache("confirmation", "oui"), "Confirmer l’envoi"),
                l=lien(base, "Annuler", "bouton bouton-discret")),
    ])


# ---------------------------------------------------------------------------
# File humaine
# ---------------------------------------------------------------------------


def page_non_rattaches(ctx: Contexte, entrees: Sequence[tuple[str, DecisionRoutage]],
                       pieces: Mapping[tuple[str, str], PieceAttendue]) -> Html:
    lignes = []
    for cle, d in entrees:
        if d.statut is StatutRoutage.PROPOSEE:
            p = pieces.get((d.dossier or "", d.reference_operation or ""))
            proposition = (gabarit('{d}/{r} — {l} — {m}', d=p.dossier, r=p.reference, l=p.libelle,
                                   m=montant(p.montant, p.devise))
                           if p else gabarit("{d}/{r}", d=d.dossier, r=d.reference_operation))
            situation = gabarit('<span class="etat statut-proposee">Proposition</span><br>{p}', p=proposition)
            action = formulaire("/non-rattaches/confirmer", ctx.csrf, champ_cache("cle", cle),
                                "Confirmer le rattachement")
        else:
            motif = LIBELLES_MOTIF.get(d.motif, d.motif.value if d.motif else "non routé")
            situation = gabarit('<span class="etat statut-non-routee">Non routée</span><br>{m}', m=motif)
            champs = joindre([
                champ_cache("cle", cle),
                champ_texte("dossier", "Dossier", f"dossier-{cle[:16]}", requis=True, valeur=d.dossier or "", max_=100),
                champ_texte("reference", "Référence d’opération", f"ref-{cle[:16]}", requis=True, max_=100),
            ])
            action = formulaire("/non-rattaches/confirmer", ctx.csrf, champs, "Rattacher",
                                classe="formulaire formulaire-compact")
        lignes.append(ligne(d.nom_fichier or "—", d.message_id, situation, d.detail or "—", action))
    return joindre([
        gabarit("<p>{t}</p>", t="Chaque pièce reçue par e-mail attend ici une décision humaine : le système "
                "ne rattache jamais seul. Vérifiez le dossier et l’opération avant de confirmer."),
        tableau("Pièces reçues à rattacher", ["Fichier", "Message-ID", "Situation", "Détail", "Décision"],
                lignes, vide="Aucun message en attente."),
    ])


# ---------------------------------------------------------------------------
# Pieces
# ---------------------------------------------------------------------------


def _formulaires_piece(ctx: Contexte, p: PieceAttendue, retour: Mapping[str, str]) -> Html:
    if p.etat in ETATS_TERMINAUX:
        return Html('<span class="secondaire">—</span>')
    ident = hashlib.sha256(f"{p.dossier}\0{p.reference}".encode("utf-8", "surrogatepass")).hexdigest()[:12]
    commun = joindre([champ_cache("dossier", p.dossier), champ_cache("reference", p.reference)]
                     + [champ_cache(k, v) for k, v in retour.items()])
    formes: list[Html] = []
    if p.etat is EtatPiece.RECUE and not p.bloquee:
        formes.append(formulaire("/pieces/controler", ctx.csrf,
                                 joindre([commun, champ_cache("decision", "conforme")]), "Contrôle conforme"))
        formes.append(formulaire("/pieces/controler", ctx.csrf, joindre([
            commun, champ_cache("decision", "non_conforme"),
            champ_texte("motif", "Motif de non-conformité", f"nc-{ident}", requis=True)]),
            "Non conforme", classe_bouton="bouton bouton-danger"))
    if p.etat is EtatPiece.DEMANDEE and not p.bloquee:
        formes.append(formulaire("/pieces/promesse", ctx.csrf, joindre([
            commun, champ_texte("date", "Date promise par le client", f"pr-{ident}", requis=True,
                                type_="date", max_=10)]), "Enregistrer la promesse"))
    if p.etat is EtatPiece.ESCALADEE and ctx.utilisateur is not None and ctx.utilisateur.est_responsable:
        formes.append(formulaire("/pieces/arbitrer", ctx.csrf, joindre([
            commun, champ_texte("motif", "Motif du classement", f"ar-{ident}", requis=True)]),
            "Classer sans suite", classe_bouton="bouton bouton-danger"))
    if p.bloquee:
        formes.append(formulaire("/pieces/debloquer", ctx.csrf, joindre([
            commun, champ_texte("motif", "Motif du déblocage", f"db-{ident}", requis=True)]), "Débloquer"))
    else:
        formes.append(formulaire("/pieces/bloquer", ctx.csrf, joindre([
            commun, champ_texte("motif", "Motif du blocage", f"bl-{ident}", requis=True)]),
            "Bloquer", classe_bouton="bouton bouton-discret"))
    return gabarit('<details class="actions"><summary>Actions</summary>{f}</details>', f=joindre(formes))


def page_pieces(ctx: Contexte, lignes_suivi: Sequence[LigneSuivi], *, dossiers: Sequence[str],
                dossier: str, etat: str) -> Html:
    filtre = gabarit(
        '<form class="filtres" method="get" action="/pieces">{d}{e}'
        '<button type="submit" class="bouton bouton-discret">Filtrer</button></form>',
        d=filtre_select("dossier", "Dossier", ((x, x) for x in dossiers), dossier),
        e=filtre_select("etat", "État", ((e.value, LIBELLES_ETAT[e]) for e in EtatPiece), etat),
    )
    retour = {"f_dossier": dossier, "f_etat": etat}
    lignes = []
    for l in lignes_suivi:
        p = l.piece
        motif = gabarit('<br><span class="secondaire">{m}</span>', m=p.motif_blocage) if p.bloquee else Html("")
        lignes.append(ligne(
            p.dossier, p.reference, date_fr(p.date_operation), p.libelle, montant(p.montant, p.devise),
            joindre([badge_etat(p.etat, p.bloquee), motif]), str(p.nb_relances),
            date_fr(l.echeance) if l.echeance else "—", l.action,
            _formulaires_piece(ctx, p, retour),
            classes=("code", "code", "date", "", "nombre", "", "nombre", "date", "", ""),
        ))
    return joindre([filtre, tableau(
        "Pièces attendues",
        ["Dossier", "Référence", "Date d’opération", "Libellé bancaire", ("Montant", "nombre"), "État",
         ("Relances", "nombre"), "Échéance", "Prochaine étape", "Actions"],
        lignes, vide="Aucune pièce pour ce filtre.")])


# ---------------------------------------------------------------------------
# Escalades
# ---------------------------------------------------------------------------


def page_escalades(ctx: Contexte, pieces: Sequence[PieceAttendue], incertains: Sequence[Brouillon]) -> Html:
    responsable = ctx.utilisateur is not None and ctx.utilisateur.est_responsable
    lignes = []
    for i, p in enumerate(pieces):
        if responsable:
            action = formulaire("/pieces/arbitrer", ctx.csrf, joindre([
                champ_cache("dossier", p.dossier), champ_cache("reference", p.reference),
                champ_cache("retour", "escalades"),
                champ_texte("motif", "Motif du classement", f"esc-{i}", requis=True)]),
                "Classer sans suite", classe_bouton="bouton bouton-danger", classe="formulaire formulaire-compact")
        else:
            action = gabarit('<span class="secondaire">{t}</span>', t="Arbitrage réservé au responsable")
        lignes.append(ligne(p.dossier, p.reference, date_fr(p.date_operation), p.libelle,
                            montant(p.montant, p.devise), str(p.nb_relances), date_fr(p.date_premiere_demande),
                            action, classes=("code", "code", "date", "", "nombre", "nombre", "date", "")))
    lignes_inc = [
        ligne(lien(f"/brouillons/{b.id_relance}", b.objet or b.id_relance), b.destinataire, b.valide_par,
              badge_brouillon(b.statut))
        for b in incertains
    ]
    return joindre([
        gabarit("<p>{t}</p>", t="Pièces restées sans réponse après toutes les relances. Le responsable peut "
                "les classer sans suite (motif obligatoire) ; une pièce reçue entre-temps se rattache depuis "
                "la file des messages non rattachés."),
        tableau("Pièces escaladées", ["Dossier", "Référence", "Date d’opération", "Libellé bancaire",
                                      ("Montant", "nombre"), ("Relances", "nombre"), "Première demande", "Décision"],
                lignes, vide="Aucune pièce escaladée."),
        gabarit("<h2>{t}</h2>", t="Envois incertains à trancher"),
        tableau("Envois incertains", ["Objet", "Destinataire", "Validé par", "Statut"], lignes_inc,
                vide="Aucun envoi incertain."),
    ])


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def _details_audit(details: Mapping[str, Any]) -> str:
    texte = json.dumps(details, ensure_ascii=False, sort_keys=True, separators=(", ", ": "))
    return texte if len(texte) <= 400 else texte[:397] + "..."


def page_audit(ctx: Contexte, verification: ResultatVerification, entrees: Sequence[EntreeAudit] | None,
               erreur_lecture: str = "", total: int = 0) -> Html:
    if verification.ok:
        etat = gabarit('<p><span class="etat etat-ok">Intègre</span> Chaîne de hachage vérifiée : {n} entrée(s). {r}</p>',
                       n=verification.nb_entrees, r=verification.raison)
    else:
        etat = gabarit('<p><span class="etat etat-alerte">Altéré</span> {r}</p>'
                       '<p class="secondaire">{n} entrée(s) saine(s) avant la première erreur.</p>',
                       r=verification.raison, n=verification.nb_entrees)
    actions = [formulaire("/audit/exporter", ctx.csrf, Html(""), "Exporter le dépôt (JSON)")]
    if not verification.ok:
        actions.append(gabarit(
            '<p class="secondaire">{t}</p>', t="La réparation retire UNIQUEMENT une dernière ligne tronquée "
            "(coupure pendant une écriture) et la sauvegarde à côté du journal. Toute autre altération est "
            "refusée : garder une copie du fichier et enquêter."))
        actions.append(formulaire("/audit/reparer", ctx.csrf, Html(""), "Réparer la fin du journal",
                                  classe_bouton="bouton bouton-danger"))
    lignes = [
        ligne(str(e.seq), e.horodatage, e.acteur, e.action, e.objet, _details_audit(e.details),
              classes=("nombre", "", "", "", "", "details"))
        for e in (entrees or ())
    ]
    titre_table = (f"Dernières entrées ({len(lignes)} sur {total})" if total > len(lignes)
                   else "Entrées du journal")
    return joindre([
        gabarit('<div class="deux-colonnes">{a}{b}</div>', a=carte("Vérification", etat),
                b=carte("Actions", joindre(actions))),
        avis(erreur_lecture, "erreur") if erreur_lecture else Html(""),
        tableau(titre_table, [("N°", "nombre"), "Horodatage", "Acteur", "Action", "Objet", "Détails"],
                lignes, vide="Journal vide."),
    ])


def page_resultat(ctx: Contexte, titre: str, message: str, retour: str) -> Html:
    return document(ctx, titre, joindre([avis(message), gabarit("<p>{l}</p>", l=lien(retour, "Continuer"))]))


__all__ = [
    "BANDEAU_MODE_TEST", "Contexte", "document", "message_fait", "page_accueil", "page_audit",
    "page_brouillon", "page_brouillons", "page_confirmation_envoi", "page_connexion", "page_deconnexion",
    "page_erreur", "page_escalades", "page_non_rattaches", "page_pieces", "page_resultat",
]
