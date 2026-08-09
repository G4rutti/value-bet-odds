"""Scanner de Super Odds da Betano."""

# O .env é carregado aqui, no import do pacote, porque `config.py` (e o
# `value/config.py`) leem os.getenv já no nível de módulo. Carregar depois não
# teria efeito nenhum — as constantes já estariam congeladas.
from .dotenv import carregar as _carregar_env

_carregar_env()
