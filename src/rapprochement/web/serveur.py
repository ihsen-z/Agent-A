"""Serveur HTTP local : `wsgiref.simple_server`, un fil d'execution par requete.

Aucun module POSIX : fonctionne sous Windows. Ecoute sur 127.0.0.1 par defaut ;
une adresse non loopback n'est utilisee que si l'appelant la demande explicitement
(`scripts/web.py --hote`), avec un avertissement.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import socketserver
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from .application import Application

HOTE_DEFAUT = "127.0.0.1"
PORT_DEFAUT = 8000


def est_loopback(hote: str) -> bool:
    if hote.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hote.strip("[]")).is_loopback
    except ValueError:
        return False


class _ServeurThreade(socketserver.ThreadingMixIn, WSGIServer):
    daemon_threads = True
    # Sous Windows, SO_REUSEADDR permettrait a un autre programme de partager le port.
    allow_reuse_address = os.name != "nt"


class _ServeurThreade6(_ServeurThreade):
    address_family = socket.AF_INET6


def _gestionnaire(journal: logging.Logger) -> type[WSGIRequestHandler]:
    class Gestionnaire(WSGIRequestHandler):
        """Journal d'acces dans logs/web.log (ligne de requete et statut, jamais les en-tetes)."""

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - signature imposee
            try:
                journal.info("acces %s %s", self.client_address[0], format % args)
            except Exception:  # noqa: BLE001
                pass

    return Gestionnaire


def creer_serveur(application: Application, hote: str = HOTE_DEFAUT, port: int = PORT_DEFAUT) -> WSGIServer:
    classe = _ServeurThreade6 if ":" in hote else _ServeurThreade
    return make_server(hote.strip("[]"), port, application, server_class=classe,
                       handler_class=_gestionnaire(application.journal))


__all__ = ["HOTE_DEFAUT", "PORT_DEFAUT", "creer_serveur", "est_loopback"]
