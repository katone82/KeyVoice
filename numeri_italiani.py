"""
Conversione numeri interi in parole italiane (1-60) e viceversa.

Usato per generare la grammatica Vosk del timer vocale
(vosk_listener.py) e per estrarre la durata da un comando
già riconosciuto (fuzzy_parser.py).
"""

_UNITA = [
    "", "uno", "due", "tre", "quattro", "cinque",
    "sei", "sette", "otto", "nove",
]

_SPECIALI = {
    10: "dieci",
    11: "undici",
    12: "dodici",
    13: "tredici",
    14: "quattordici",
    15: "quindici",
    16: "sedici",
    17: "diciassette",
    18: "diciotto",
    19: "diciannove",
}

_DECINE = {
    2: "venti",
    3: "trenta",
    4: "quaranta",
    5: "cinquanta",
    6: "sessanta",
}


def numero_in_parole(n: int) -> str:
    if 1 <= n <= 9:
        return _UNITA[n]

    if n in _SPECIALI:
        return _SPECIALI[n]

    decina, unita = divmod(n, 10)
    base = _DECINE[decina]

    if unita == 0:
        return base

    # ventuno, ventotto, trentuno, trentotto, ...
    if unita in (1, 8):
        base = base[:-1]

    return base + _UNITA[unita]


def parola_numero_singolare(n: int) -> str:
    """
    Forma usata subito prima di un nome singolare
    (es. "un minuto", "un ora") invece di "uno minuto".
    """

    if n == 1:
        return "un"

    return numero_in_parole(n)


PAROLA_A_NUMERO = {
    numero_in_parole(n): n
    for n in range(1, 61)
}

PAROLA_A_NUMERO["un"] = 1
PAROLA_A_NUMERO["uno"] = 1
