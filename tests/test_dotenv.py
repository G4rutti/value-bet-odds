"""Testes do leitor/escritor de .env e da descoberta de chat_id."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from betano_superodds import dotenv
from betano_superodds.notifier import TelegramNotifier


class TestParse(unittest.TestCase):
    def test_formatos_aceitos(self):
        valores = dotenv.parse(
            "\n".join([
                "# comentário",
                "",
                "SIMPLES=valor",
                '  COM_ASPAS = "com espaço"  ',
                "COM_SIMPLES='outro'",
                "export COM_EXPORT=abc",
                "COM_COMENTARIO=abc   # sobra",
                "IGUAL_NO_VALOR=a=b=c",
                "linha inválida sem igual",
                "VAZIO=",
            ])
        )
        self.assertEqual(valores["SIMPLES"], "valor")
        self.assertEqual(valores["COM_ASPAS"], "com espaço")
        self.assertEqual(valores["COM_SIMPLES"], "outro")
        self.assertEqual(valores["COM_EXPORT"], "abc")
        self.assertEqual(valores["COM_COMENTARIO"], "abc")
        self.assertEqual(valores["IGUAL_NO_VALOR"], "a=b=c")
        self.assertEqual(valores["VAZIO"], "")
        self.assertNotIn("linha", valores)

    def test_cerquilha_dentro_de_aspas_nao_e_comentario(self):
        """Token do Telegram não tem '#', mas senha de proxy pode ter."""
        self.assertEqual(dotenv.parse('X="a#b"')["X"], "a#b")


class TestCarregar(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.arquivo = Path(self._tmp.name) / ".env"
        self._salvos = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._salvos)
        self._tmp.cleanup()

    def test_carrega_para_o_ambiente(self):
        self.arquivo.write_text("TESTE_DOTENV_A=1\n", encoding="utf-8")
        dotenv.carregar(self.arquivo)
        self.assertEqual(os.environ["TESTE_DOTENV_A"], "1")

    def test_nao_sobrescreve_variavel_ja_definida(self):
        """Quem exportou na mão (ou o Docker) manda mais que o arquivo."""
        os.environ["TESTE_DOTENV_B"] = "do_ambiente"
        self.arquivo.write_text("TESTE_DOTENV_B=do_arquivo\n", encoding="utf-8")
        dotenv.carregar(self.arquivo)
        self.assertEqual(os.environ["TESTE_DOTENV_B"], "do_ambiente")

    def test_sobrescrever_explicito(self):
        os.environ["TESTE_DOTENV_C"] = "velho"
        self.arquivo.write_text("TESTE_DOTENV_C=novo\n", encoding="utf-8")
        dotenv.carregar(self.arquivo, sobrescrever=True)
        self.assertEqual(os.environ["TESTE_DOTENV_C"], "novo")

    def test_arquivo_ausente_nao_quebra(self):
        self.assertEqual(dotenv.carregar(Path(self._tmp.name) / "nao_existe"), {})


class TestEscrever(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.arquivo = Path(self._tmp.name) / ".env"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_cria_arquivo_novo(self):
        dotenv.escrever({"TELEGRAM_BOT_TOKEN": "abc"}, self.arquivo)
        self.assertEqual(dotenv.parse(self.arquivo.read_text(encoding="utf-8")),
                         {"TELEGRAM_BOT_TOKEN": "abc"})

    def test_atualiza_chave_preservando_o_resto(self):
        self.arquivo.write_text(
            "# meu comentário\nOUTRA=mantida\nTELEGRAM_BOT_TOKEN=velho\n",
            encoding="utf-8")
        dotenv.escrever({"TELEGRAM_BOT_TOKEN": "novo"}, self.arquivo)
        texto = self.arquivo.read_text(encoding="utf-8")
        self.assertIn("# meu comentário", texto)
        valores = dotenv.parse(texto)
        self.assertEqual(valores["OUTRA"], "mantida")
        self.assertEqual(valores["TELEGRAM_BOT_TOKEN"], "novo")

    def test_acrescenta_chave_que_ainda_nao_existe(self):
        self.arquivo.write_text("TELEGRAM_BOT_TOKEN=t\n", encoding="utf-8")
        dotenv.escrever({"TELEGRAM_CHAT_ID": "123"}, self.arquivo)
        valores = dotenv.parse(self.arquivo.read_text(encoding="utf-8"))
        self.assertEqual((valores["TELEGRAM_BOT_TOKEN"], valores["TELEGRAM_CHAT_ID"]),
                         ("t", "123"))

    def test_ida_e_volta_com_valor_espinhoso(self):
        dotenv.escrever({"X": "com espaço e # tralha"}, self.arquivo)
        self.assertEqual(dotenv.parse(self.arquivo.read_text(encoding="utf-8"))["X"],
                         "com espaço e # tralha")


class TestDescobrirChatId(unittest.TestCase):
    def _notifier(self, resposta: dict | None) -> TelegramNotifier:
        n = TelegramNotifier(token="tok", chat_id="")
        n._chamar = lambda metodo, **kw: resposta  # type: ignore[assignment]
        return n

    def test_extrai_da_mensagem_mais_recente(self):
        n = self._notifier({"ok": True, "result": [
            {"message": {"chat": {"id": 111}}},
            {"message": {"chat": {"id": 222}}},
        ]})
        self.assertEqual(n.descobrir_chat_id(), "222")

    def test_aceita_edited_message_e_channel_post(self):
        self.assertEqual(
            self._notifier({"ok": True, "result": [
                {"edited_message": {"chat": {"id": 5}}}]}).descobrir_chat_id(), "5")
        self.assertEqual(
            self._notifier({"ok": True, "result": [
                {"channel_post": {"chat": {"id": -100}}}]}).descobrir_chat_id(), "-100")

    def test_sem_update_devolve_none(self):
        self.assertIsNone(self._notifier({"ok": True, "result": []}).descobrir_chat_id())

    def test_falha_de_rede_devolve_none(self):
        self.assertIsNone(self._notifier(None).descobrir_chat_id())

    def test_ignora_update_sem_chat(self):
        n = self._notifier({"ok": True, "result": [
            {"my_chat_member": {"foo": 1}},
            {"message": {"chat": {"id": 7}}},
        ]})
        self.assertEqual(n.descobrir_chat_id(), "7")

    def test_sem_token_nao_chama_a_api(self):
        self.assertIsNone(TelegramNotifier(token="", chat_id="").identidade())


if __name__ == "__main__":
    unittest.main(verbosity=2)
