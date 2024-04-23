from langchain.prompts import PromptTemplate
from pydantic import BaseModel

from agentblocks.docconveyer import DocConveyer, break_up_big_docs
from agentblocks.webprocess import URLConveyer
from agentblocks.webretrieve import get_content_from_urls
from agentblocks.websearch import get_web_search_result_urls_from_prompt
from utils.chat_state import ChatState
from utils.helpers import format_nonstreaming_answer
from utils.prepare import CONTEXT_LENGTH
from utils.type_utils import JSONishDict, Props

query_generator_template = """# MISSION
You are an advanced assistant in satisfying USER's information need.

# INPUT 
You will be provided with a query from USER and the current timestamp.

# HIGH LEVEL TASK
You don't need to answer the query. Instead, your goal is to determine the information need behind the query and figure out the best possible Google search queries to find the best website that contains what USER needs.

# OUTPUT
Your output should be JSON in the following format:

{{"queries": [<array of 3-7 Google search queries that would be most helpful to perform in order to find the best single website that contains the perfect objective, unbiased, up-to-date answer to USER's query>]}}

## EXAMPLES OF OUTPUT 

query: "How do I start with Langchain? I want to use it to make a chatbot that I can deploy on a website."
timestamp: Thursday, March 13, 2025, 04:40 PM

output: {{"queries": ["langchain chatbot tutorial March 2025", "langchain getting started chatbot", "deploy langchain chatbot on website"]}}

query: "openai news"
timestamp: Saturday, June 22, 2024, 11:01 AM

output: {{"queries": ["openai news June 22 2024", "news about OpenAI June 2024", "recent OpenAI developments"]}}

query: "how can I merge two dictionaries in python?"
timestamp: Saturday, November 08, 2025, 06:04 PM

output: {{"queries": ["python merge dictionaries", "python 2025 dictionary union"]}}

query: "could you tell me the best way to treat chronic migraines"
timestamp: Monday, August 12, 2024, 11:15 PM

output: {{"queries": ["chronic migraines treatment", "evidence-based modern chronic migraine treatments", "science-based treatment chronic migraine 2024", "chronic migraines recent research August 2024"]}}

query: "I need a code example of how to use Slider shadcn/ui component in React that shows how to update state"
timestamp: Tuesday, September 12, 2023, 07:39 AM

output: {{"queries": ["shadcn ui Slider example", "shadcn \\"Slider\\" component React state update", "shadcn \\"Slider\\" controlled component React example", \\"Slider\\" uncontrolled component React example", "shadcn ui Slider tutorial"]}}

# YOUR ACTUAL OUTPUT
query: "{query}"
timestamp: {timestamp}

output: """
hs_query_generator_prompt = PromptTemplate.from_template(query_generator_template)

search_queries_updater_template = """\
You are an advanced assistant in satisfying USER's information need.

# High Level Task

You will be provided information about USER's query and current state of formulating the answer. Your task is to determine what needs to be added or improved in order to better satisfy USER's information need and strategically design a list of google search queries that would be most helpful to perform.

# Input

1. USER's query: {query} 
END OF USER's query 

2. Current timestamp: {timestamp}
END OF timestamp

3. Requested answer format: {report_type}
END OF requested answer format

4. Google search queries used to generate the current draft of the answer: {search_queries}
END OF search queries

5. Current draft of the answer: {report}

# Detailed Task

Let's work step by step. First, you need to determine what needs to be added or improved in order to better satisfy USER's information need. Then, based on the results of your analysis, you need to strategically design a list of google search queries that would be most helpful to perform to get an accurate, complete, unbiased, up-to-date answer. Design these queries so that the google search results will provide the necessary information to fill in any gaps in the current draft of the answer, or improve it in any way.

Use everything you know about information foraging and information literacy in this task.

# Output

Your output should be in JSON in the following format:

{{"analysis": <brief description of what kind of information we should be looking for to improve the answer and why you think the previous google search queries may not have yielded that information>,
"queries": [<array of 3-7 new google search queries that would be most helpful to perform, based on that analysis>]}}

# Example

Suppose the user wants to get a numbered list of top Slavic desserts and you notice that the current draft includes desserts from Russia and Ukraine, but is missing desserts from other, non-former-USSR Slavic countries. You would then provide appropriate analysis and design new google search queries to fill in that gap, for example your output could be:

{{"analysis": "The current draft of the answer is missing desserts from other Slavic countries besides Russia and Ukraine. The current search queries seem to have resulted in content being mostly about countries from the former USSR so we should specifically target other Slavic countries.",
"queries": ["top desserts Poland", "top desserts Czech Republic", "top desserts Slovakia", "top desserts Bulgaria", "best desserts from former Yugoslavia", "desserts from Easern Europe"]}}

# Your actual output

Now, please use the information in the "# Input" section to construct your actual output, which should start with the opening curly brace and end with the closing curly brace:


"""
hs_query_updater_prompt = PromptTemplate.from_template(search_queries_updater_template)

hs_answer_generator_template = """\
{context}

END OF PROVIDED CONTENT

TASK: Determine if the above provided content contains a full, accurate, unbiased, and up-to-date answer to user's query:

{query}

END OF QUERY
If the provided content contains information sufficient to construct a full, accurate, unbiased, and up-to-date answer to user's query, then your task is to answer the query. If the provided content is insufficient, then simply reply with one word: "CONTENT_INSUFFICIENT".
"""
hs_answer_generator_prompt = PromptTemplate.from_template(hs_answer_generator_template)

class HeatseekData(BaseModel):
    query: str
    num_iterations_left: int
    url_conveyer: URLConveyer
    doc_conveyer: DocConveyer
    is_done: bool
    answer: str

def get_new_heatseek_response(chat_state: ChatState) -> Props:
    query = chat_state.message
    num_iterations = chat_state.parsed_query.research_params.num_iterations_left

    # Get links from prompt
    urls = get_web_search_result_urls_from_prompt(
        hs_query_generator_prompt,
        inputs={"query": query},
        num_links=100,
        chat_state=chat_state,
    )

    # Get content from links and initialize URLConveyer
    url_retrieval_data = get_content_from_urls(urls, min_ok_urls=5)
    url_conveyer = URLConveyer.from_retrieval_data(url_retrieval_data)

    # Convert retrieval data to docs and break up docs that are too big
    docs = url_conveyer.get_next_docs()
    docs = break_up_big_docs(docs, max_tokens=CONTEXT_LENGTH * 0.25)

    # Put docs in DocConveyer and get the first batch of docs (in heatseek,
    # we only get one full doc at a time, but if it's big, it can come in parts)
    doc_conveyer = DocConveyer(docs)
    docs = doc_conveyer.get_next_docs(max_tokens=CONTEXT_LENGTH * 0.5, max_full_docs=1)

    # Construct the context and get response from LLM
    context = f"SOURCE: {docs[0].metadata['source']}\n\n{''.join(doc.page_content for doc in docs)}"
    inputs = {"query": query, "context": context}
    reply = chat_state.get_llm_reply(hs_answer_generator_prompt, inputs, to_user=True)

    # Parse response and construct agent data
    is_done = "CONTENT_INSUFFICIENT" not in reply
    answer = reply if is_done else ""
    hs_data = HeatseekData(
        query=query,
        num_iterations_left=num_iterations - 1,
        url_conveyer=url_conveyer,
        doc_conveyer=doc_conveyer,
        is_done=is_done,
        answer=answer,
    )

    #...
    
    # Return response
    return {"answer": answer}


def get_heatseek_in_progress_response(
    chat_state: ChatState, hs_data: JSONishDict
) -> Props:
    message = chat_state.message
    num_iterations = chat_state.parsed_query.research_params.num_iterations_left
    return format_nonstreaming_answer(
        f"NOT YET IMPLEMENTED: Your heatseek is in progress. {num_iterations} iterations left."
    )


# NOTE: should catch and handle exceptions in main handler
def get_research_heatseek_response(chat_state: ChatState) -> Props:
    if chat_state.message:
        return get_new_heatseek_response(chat_state)

    hs_data = chat_state.agent_data.get("hs_data")
    if hs_data:
        return get_heatseek_in_progress_response(chat_state, hs_data)

    return format_nonstreaming_answer(
        "To start a new heatseek, type `/re heatseek <your query>`."
    )
