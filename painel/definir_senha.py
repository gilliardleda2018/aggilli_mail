#!/usr/bin/env python3
"""Define (ou troca) a senha de acesso ao painel. Rode no terminal: python3 painel/definir_senha.py"""
import getpass
import sys

import auth

p1 = getpass.getpass("Nova senha do painel: ")
if len(p1) < 10:
    sys.exit("Use pelo menos 10 caracteres.")
if getpass.getpass("Repita a senha: ") != p1:
    sys.exit("As senhas não conferem.")
auth.save(p1)
print("Senha salva.")
