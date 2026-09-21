from app.llm.factory import get_chat_model
from app.prompts.extract import EXTRACT_PROMPT
from app.schemas.aftersales import AfterSalesExtract


async def extract_aftersales(text: str) -> AfterSalesExtract:
    llm = get_chat_model().with_structured_output(
        AfterSalesExtract,
        method="json_schema",
    )
    return await (EXTRACT_PROMPT | llm).ainvoke({"text": text})
