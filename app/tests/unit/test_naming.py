from app.infrastructure.ai.llm.base import LLMResult, LLMUnavailableError
from app.modules.ingestion.analysis.metadata import Detected
from app.modules.ingestion.analysis.naming import ContentRead, content_title, describe, key_phrases, read_content

COVER = "State Bank of India\nPolicy on\n‘Microfinance Loans’\nPolicy uploaded in in SBI Times (Path: SBI Times > Manuals)"


class Answers:
    def __init__(self, name, about=None):
        self.name, self.about = name, about

    def generate_json(self, system, user, schema, *, context=None):
        return LLMResult(content={"name": self.name, "about": self.about}, model="fake")


def test_the_model_names_the_document_from_its_content():
    read = read_content(lambda: Answers("Policy on Microfinance Loans"), COVER, "Policy Uploaded in in SBI Times", None)
    assert read.name == "Policy on Microfinance Loans"


def test_a_name_with_words_not_in_the_document_is_refused():
    assert read_content(lambda: Answers("Microcredit Lending Policy"), COVER, None, None).name is None


def test_the_pdf_title_counts_as_the_documents_own_words():
    read = read_content(lambda: Answers("Code of Ethics"), "OUR CODE OF E HICS", None, "Code of Ethics 2.0 booklet")
    assert read.name == "Code of Ethics"


def test_without_a_model_the_detected_title_stands():
    def unavailable():
        raise LLMUnavailableError("OPENAI_API_KEY is not configured.")
    assert read_content(unavailable, COVER, "Policy on Microfinance Loans", None) == ContentRead()
    assert read_content(lambda: Answers(None), COVER, None, None) == ContentRead()


def test_what_the_document_is_about_never_states_a_figure_it_does_not():
    text = f"{COVER}\nLoans up to Rs. 3,00,000 for households."
    assert read_content(lambda: Answers(None, "loans up to Rs. 3,00,000 for households"), text, None, None).about \
        == "loans up to Rs. 3,00,000 for households"
    assert read_content(lambda: Answers(None, "loans up to Rs. 5,00,000"), text, None, None).about is None


def test_a_name_the_layout_also_found_keeps_its_evidence():
    detected = Detected("Home Loan Credit Policy", 0.85, "largest_font", "HOME LOAN CREDIT POLICY")
    assert content_title("Home Loan Credit Policy", detected) is detected
    named = content_title("Policy on Microfinance Loans", detected)
    assert (named.value, named.source, named.evidence) == ("Policy on Microfinance Loans", "document_content", None)
    assert content_title(None, detected) is None


KYC = """Know Your Customer (KYC) Policy
The Bank shall follow the customer identification procedure. Money laundering risk is assessed.
Customer identification procedure applies at onboarding; money laundering alerts are reviewed.
Under the customer identification procedure, money laundering and terrorist financing are reported.
The KYC Policy is reviewed by the Board. The KYC Policy applies to all branches."""


def test_key_phrases_say_what_the_document_covers_not_its_name():
    phrases = key_phrases(KYC, "Know Your Customer (KYC) Policy")
    assert phrases == ["customer identification procedure", "money laundering"]


def test_the_description_prefers_the_model_then_key_phrases():
    assert describe(ContentRead(about="customer due diligence"), KYC, None) == "customer due diligence"
    assert describe(ContentRead(), KYC, "Know Your Customer (KYC) Policy") == "customer identification procedure and money laundering"
    assert describe(ContentRead(), "A short note.", None) is None
