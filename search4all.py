import concurrent.futures
import json
import os
import re
import requests
import traceback
import httpx
from typing import AsyncGenerator
from openai import AsyncOpenAI
import asyncio
from anthropic import AsyncAnthropic
from loguru import logger
from dotenv import load_dotenv
import urllib.parse
import trafilatura
from trafilatura import bare_extraction
import tldextract
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
from serpapi import GoogleSearch
load_dotenv()

import sanic
from sanic import Sanic
import sanic.exceptions
from sanic.exceptions import HTTPException, InvalidUsage
from sqlitedict import SqliteDict

app = Sanic("search")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# -------------------- Fanout config & helpers (ADDED) --------------------
FASTLY_SERVICE_ID = os.getenv("FASTLY_SERVICE_ID")
FASTLY_API_TOKEN  = os.getenv("FASTLY_API_TOKEN")
FANOUT_ENABLED    = str(os.getenv("FANOUT_ENABLED", "0")).lower() in ("1", "true", "yes")
PUBLISH_ENDPOINT  = f"https://api.fastly.com/service/{FASTLY_SERVICE_ID}/publish/" if FASTLY_SERVICE_ID else None

def sse_event(event_type: str, data: dict | str) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"event: {event_type}\ndata: {payload}\n\n"

def fanout_publish(channel: str, content: str) -> None:
    if not (FANOUT_ENABLED and FASTLY_SERVICE_ID and FASTLY_API_TOKEN and PUBLISH_ENDPOINT):
        return
    body = {
        "items": [{
            "channel": channel,
            "formats": {
                "http-stream": {
                    "content": content if content.endswith("\n\n") else (content + "\n")
                }
            }
        }]
    }
    headers = {"Fastly-Key": FASTLY_API_TOKEN, "Content-Type": "application/json"}
    try:
        r = requests.post(PUBLISH_ENDPOINT, headers=headers, data=json.dumps(body), timeout=5)
        r.raise_for_status()
    except Exception as e:
        # Non-fatal: don’t break origin response if publish fails
        logger.warning(f"Fanout publish failed: {e}")
# ------------------------------------------------------------------------

################################################################################
# Constant values for the RAG model.
################################################################################

BING_SEARCH_V7_ENDPOINT = "https://api.bing.microsoft.com/v7.0/search"
BING_MKT = "en-US"
GOOGLE_SEARCH_ENDPOINT = "https://customsearch.googleapis.com/customsearch/v1"
SERPER_SEARCH_ENDPOINT = "https://google.serper.dev/search"
SEARCHAPI_SEARCH_ENDPOINT = "https://www.searchapi.io/api/v1/search"
SEARCH1API_SEARCH_ENDPOINT = "https://api.search1api.com/search/"

REFERENCE_COUNT = 8
DEFAULT_SEARCH_ENGINE_TIMEOUT = 5
MAX_HISTORY_LEN = 10

_default_query = "Who said 'live long and prosper'?"

_rag_query_text = """
You are a large language AI assistant built by AI. You are given a user question, and please write clean, concise and accurate answer to the question. You will be given a set of related contexts to the question, each starting with a reference number like [[citation:x]], where x is a number. Please use the context and cite the context at the end of each sentence if applicable.

Your answer must be correct, accurate and written by an expert using an unbiased and professional tone. Please limit to 1024 tokens. Do not give any information that is not related to the question, and do not repeat. Say "information is missing on" followed by the related topic, if the given context do not provide sufficient information.

Please cite the contexts with the reference numbers, in the format [citation:x]. If a sentence comes from multiple contexts, please list all applicable citations, like [citation:3][citation:5]. Other than code and specific names and citations, your answer must be written in the same language as the question.

Here are the set of contexts:

{context}

Remember, don't blindly repeat the contexts verbatim. And here is the user question:
"""

stop_words = [
    "<|im_end|>",
    "[End]",
    "[end]",
    "\nReferences:\n",
    "\nSources:\n",
    "End.",
]

class KVWrapper(object):
    def __init__(self, kv_name):
        self._db = SqliteDict(filename=kv_name)

    def get(self, key: str):
        v = self._db[key]
        if v is None:
            raise KeyError(key)
        return v

    def put(self, key: str, value: str):
        self._db[key] = value
        self._db.commit()
    
    def append(self, key: str, value):
        """ 记录聊天历史 """
        self._db[key] = self._db.get(key, [])
        _ = self._db[key][-MAX_HISTORY_LEN:]
        _.append(value)
        self._db[key] = _
        self._db.commit()

# 格式化输出部分
def extract_all_sections(text: str):
    sections_pattern = r"(.*?)__LLM_RESPONSE__(.*?)(__RELATED_QUESTIONS__(.*))?$"
    match = re.search(sections_pattern, text, re.DOTALL)
    if match:
        search_results = match.group(1).strip()
        llm_response = match.group(2).strip()
        related_questions = match.group(4).strip() if match.group(4) else ""
    else:
        search_results, llm_response, related_questions = None, None, None
    return search_results, llm_response, related_questions

def search_with_search1api(query: str, search1api_key: str):
    payload = {
        "max_results": 10,
        "query": query,
        "search_service": "google"
    }
    headers = {
        "Authorization": f"Bearer {search1api_key}",
        "Content-Type": "application/json"
    }
    response = requests.request("POST", SEARCH1API_SEARCH_ENDPOINT, json=payload, headers=headers)
    if not response.ok:
        logger.error(f"{response.status_code} {response.text}")
        raise HTTPException("Search engine error.")
    json_content = response.json()
    try:
        contexts = json_content["results"][:REFERENCE_COUNT]
        for item in contexts:
            item["name"] = item["title"]
            item["url"] = item["link"]
    except KeyError:
        logger.error(f"Error encountered: {json_content}")
        return []
    return contexts

def search_with_bing(query: str, subscription_key: str):
    params = {"q": query, "mkt": BING_MKT}
    response = requests.get(
        BING_SEARCH_V7_ENDPOINT,
        headers={"Ocp-Apim-Subscription-Key": subscription_key},
        params=params,
        timeout=DEFAULT_SEARCH_ENGINE_TIMEOUT,
    )
    if not response.ok:
        logger.error(f"{response.status_code} {response.text}")
        raise HTTPException("Search engine error.")
    json_content = response.json()
    try:
        contexts = json_content["webPages"]["value"][:REFERENCE_COUNT]
    except KeyError:
        logger.error(f"Error encountered: {json_content}")
        return []
    return contexts

def search_with_google(query: str, subscription_key: str, cx: str):
    params = {"key": subscription_key, "cx": cx, "q": query, "num": REFERENCE_COUNT}
    response = requests.get(GOOGLE_SEARCH_ENDPOINT, params=params, timeout=DEFAULT_SEARCH_ENGINE_TIMEOUT)
    if not response.ok:
        logger.error(f"{response.status_code} {response.text}")
        raise HTTPException("Search engine error.")
    json_content = response.json()
    try:
        contexts = json_content["items"][:REFERENCE_COUNT]
        for item in contexts:
            item["name"] = item["title"]
            item["url"] = item["link"]
    except KeyError:
        logger.error(f"Error encountered: {json_content}")
        return []
    return contexts

def search_with_serpapi(query: str, api_key: str):
    params = {"engine": "google", "q": query, "api_key": api_key}
    search = GoogleSearch(params)
    try:
        results = search.get_dict()
        organic_results = results.get("organic_results", [])
        contexts = [{"name": c["title"], "url": c["link"], "snippet": c.get("snippet", "")} for c in organic_results]
        return contexts[:REFERENCE_COUNT]
    except Exception as e:
        logger.error(f"Serpapi error: {e}")
        raise HTTPException("Search engine error.")

def extract_url_content(url):
    logger.info(url)
    downloaded = trafilatura.fetch_url(url)
    content = trafilatura.extract(downloaded)
    logger.info(url + "______" + str(content))
    return {"url": url, "content": content}

def search_with_searXNG(query:str, url:str):
    content_list = []
    try:
        safe_string = urllib.parse.quote_plus(":auto " + query)
        response = requests.get(url+'?q=' + safe_string + '&category=general&format=json&engines=bing%2Cgoogle')
        response.raise_for_status()
        search_results = response.json()

        pedding_urls = []
        conv_links = []
        results = []

        if search_results.get('results'):
            for item in search_results.get('results')[0:9]:
                name = item.get('title')
                snippet = item.get('content')
                url_item = item.get('url')
                pedding_urls.append(url_item)

                if url_item:
                    url_parsed = urlparse(url_item)
                    domain = url_parsed.netloc
                    icon_url = url_parsed.scheme + '://' + url_parsed.netloc + '/favicon.ico'
                    site_name = tldextract.extract(url_item).domain

                conv_links.append({
                    'site_name': site_name,
                    'icon_url': icon_url,
                    'title': name,
                    'name': name,
                    'url': url_item,
                    'snippet': snippet
                })

        if len(results) == 0:
            content_list = conv_links
        return content_list
    except Exception as ex:
        logger.error(ex)
        raise ex

def new_async_client(_app):
    if "claude-3" in _app.ctx.model.lower():
        return AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    else:
        return AsyncOpenAI(
            api_key=os.getenv("OPENAI_API_KEY") or os.getenv("GROQ_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL"),
            http_client=_app.ctx.http_session,
        )

@app.before_server_start
async def server_init(_app):
    """
    Initializes global configs.
    """
    _app.ctx.backend = os.getenv("BACKEND").upper()
    if _app.ctx.backend == "BING":
        _app.ctx.search_api_key = os.getenv("BING_SEARCH_V7_SUBSCRIPTION_KEY")
        _app.ctx.search_function = lambda query: search_with_bing(query, _app.ctx.search_api_key)
    elif _app.ctx.backend == "GOOGLE":
        _app.ctx.search_api_key = os.getenv("GOOGLE_SEARCH_API_KEY")
        _app.ctx.search_function = lambda query: search_with_google(query, _app.ctx.search_api_key, os.getenv("GOOGLE_SEARCH_CX"))
    elif _app.ctx.backend == "SERPER":
        _app.ctx.search_api_key = os.getenv("SERPER_SEARCH_API_KEY")
        _app.ctx.search_function = lambda query: search_with_serper(query, _app.ctx.search_api_key)
    elif _app.ctx.backend == "SERPAPI":
        _app.ctx.search_api_key = os.getenv("SERPAPI_API_KEY")
        _app.ctx.search_function = lambda query: search_with_serpapi(query, _app.ctx.search_api_key)
    elif _app.ctx.backend == "SEARCH1API":
        _app.ctx.search1api_key = os.getenv("SEARCH1API_KEY")
        _app.ctx.search_function = lambda query: search_with_search1api(query, _app.ctx.search1api_key)
    elif _app.ctx.backend == "SEARXNG":
        logger.info(os.getenv("SEARXNG_BASE_URL"))
        _app.ctx.search_function = lambda query: search_with_searXNG(query, os.getenv("SEARXNG_BASE_URL"))
    else:
        raise RuntimeError("Backend must be BING, GOOGLE, SERPER, SERPAPI, SEARCHAPI or SEARCH1API.")
    _app.ctx.model = os.getenv("LLM_MODEL")
    _app.ctx.handler_max_concurrency = 16
    _app.ctx.executor = concurrent.futures.ThreadPoolExecutor(max_workers=_app.ctx.handler_max_concurrency * 2)
    logger.info("Creating KV. May take a while for the first time.")
    _app.ctx.kv = KVWrapper(os.getenv("KV_NAME") or "search.db")
    _app.ctx.should_do_related_questions = bool(os.getenv("RELATED_QUESTIONS") in ("1", "yes", "true"))
    _app.ctx.should_do_chat_history = bool(os.getenv("CHAT_HISTORY") in ("1", "yes", "true"))
    _app.ctx.http_session = httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=120, write=120, pool=10))

async def get_related_questions(_app, query, contexts):
    _more_questions_prompt = r"""
    You are a helpful assistant that helps the user to ask related questions, based on user's original question and the related contexts. Please identify worthwhile topics that can be follow-ups, and write questions no longer than 20 words each. Please make sure that specifics, like events, names, locations, are included in follow up questions so they can be asked standalone. For example, if the original question asks about "the Manhattan project", in the follow up question, do not just say "the project", but use the full name "the Manhattan project". Your related questions must be in the same language as the original question.

    Here are the contexts of the question:

    {context}

    Remember, based on the original question and related contexts, suggest three such further questions. Do NOT repeat the original question. Each related question should be no longer than 20 words. Here is the original question:
    """.format(
        context="\n\n".join([c["snippet"] for c in contexts])
    )

    try:
        logger.info('Start getting related questions')
        if "claude-3" in _app.ctx.model.lower():
            logger.info('Using Claude-3 model')
            client = new_async_client(_app)
            tools = [
                {
                    "name": "ask_related_questions",
                    "description": "Get a list of questions related to the original question and context.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "questions": {
                                "type": "array",
                                "items": {"type": "string", "description": "A related question to the original question and context."}
                            }
                        },
                        "required": ["questions"]
                    }
                }
            ]
            response = await client.beta.tools.messages.create(
                model=_app.ctx.model,
                system=_more_questions_prompt,
                max_tokens=1000,
                tools=tools,
                messages=[{"role": "user", "content": query}],
            )
            logger.info('Response received from Claude-3 model')

            if response.content and len(response.content) > 0:
                related = []
                for block in response.content:
                    if block.type == "tool_use" and block.name == "ask_related_questions":
                        related = block.input["questions"]
                        break
            else:
                related = []
            if related and isinstance(related, str):
                try:
                    related = json.loads(related)
                except json.JSONDecodeError:
                    logger.error("Failed to parse related questions as JSON")
                    return []
            logger.info('Successfully got related questions')
            return [{"question": question} for question in related[:5]] 
        else:
            logger.info('Using OpenAI model')
            openai_client = new_async_client(_app)
            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": "ask_related_questions",
                        "description": "Get a list of questions related to the original question and context.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "questions": {
                                    "type": "array",
                                    "items": {"type": "string", "description": "A related question to the original question and context."}
                                }
                            },
                            "required": ["questions"]
                        }
                    }
                }
            ]
            messages = [
                {"role": "system", "content": _more_questions_prompt},
                {"role": "user", "content": query},
            ]
            request_body = {
                "model": _app.ctx.model,
                "messages": messages,
                "max_tokens": 1000,
                "tools": tools,
                "tool_choice": {"type": "function", "function": {"name": "ask_related_questions"}},
            }
            try:
                llm_response = await openai_client.chat.completions.create(**request_body)
                if llm_response.choices and llm_response.choices[0].message:
                    message = llm_response.choices[0].message
                    if message.tool_calls:
                        related = message.tool_calls[0].function.arguments
                        if isinstance(related, str):
                            related = json.loads(related)
                        logger.trace(f"Related questions: {related}")
                        return [{"question": question} for question in related["questions"][:5]]
                    elif message.content:
                        content = message.content
                        related_questions = content.split('\n')
                        related_questions = [q.strip() for q in related_questions if q.strip()]
                        cleaned_questions = []
                        for question in related_questions:
                            if question.startswith('1.') or question.startswith('2.') or question.startswith('3.'):
                                question = question[3:].strip()
                                if question.startswith('"') and question.endswith('"'):
                                    question = question[1:-1]
                                elif question.startswith('"'):
                                    question = question[1:]
                                elif question.endswith('"'):
                                    question = question[:-1]
                                cleaned_questions.append(question)
                        logger.trace(f"Related questions: {cleaned_questions}")
                        return [{"question": question} for question in cleaned_questions[:5]]
            except Exception as e:
                logger.error(f"Error occurred while sending request to OpenAI model: {str(e)}")
                return []
    except Exception as e:
        logger.error(f"Encountered error while generating related questions: {str(e)}")
        return []

# -------------------- STREAM RESPONSE (UPDATED to publish to Fanout) --------------------
async def _raw_stream_response(
    _app, search_uuid: str, contexts, llm_response, related_questions_future
) -> AsyncGenerator[str, None]:
    """
    A generator that yields the raw stream response, and mirrors events to Fanout.
    """
    # First: contexts
    ctx_json = json.dumps(contexts)
    yield ctx_json
    fanout_publish(search_uuid, sse_event("sources", contexts))

    # Marker
    marker = "\n\n__LLM_RESPONSE__\n\n"
    yield marker
    fanout_publish(search_uuid, sse_event("marker", {"t": "__LLM_RESPONSE__"}))

    # Second: LLM stream
    if not contexts:
        warn = "(The search engine returned nothing for this query. Please take the answer with a grain of salt.)\n\n"
        yield warn
        fanout_publish(search_uuid, sse_event("delta", {"text": warn}))

    if "claude-3" in _app.ctx.model.lower():
        async for text in llm_response:
            if text:
                yield text
                fanout_publish(search_uuid, sse_event("delta", {"text": text}))
    else:
        async for chunk in llm_response:
            if chunk.choices:
                t = chunk.choices[0].delta.content or ""
                if t:
                    yield t
                    fanout_publish(search_uuid, sse_event("delta", {"text": t}))

    # Third: related questions (if any)
    if related_questions_future is not None:
        try:
            related_questions = await related_questions_future
            result = json.dumps(related_questions)
        except Exception as e:
            logger.error(f"encountered error: {e}\n{traceback.format_exc()}")
            result = "[]"
        tail_marker = "\n\n__RELATED_QUESTIONS__\n\n"
        yield tail_marker
        yield result
        fanout_publish(search_uuid, sse_event("related", related_questions))

    # Done
    fanout_publish(search_uuid, sse_event("done", {}))
# ---------------------------------------------------------------------------------------

def get_query_object(request):
    params = {k: v[0] for k, v in request.args.items()}
    if request.method == "POST":
        if "form" in request.content_type:
            params.update({k: v[0] for k, v in request.form.items()})
        else:
            try:
                if request.json:
                    params.update(request.json)
            except InvalidUsage:
                pass
    return params

@app.route("/query", methods=["POST"])
async def query_function(request: sanic.Request):
    """
    Query the search engine and returns the response.
    """
    _app = request.app
    params = get_query_object(request)
    query = params.get("query", None)
    search_uuid = params.get("search_uuid", None)
    generate_related_questions = params.get("generate_related_questions", True)
    if not query:
        raise HTTPException("query must be provided.")
    if not search_uuid:
        raise HTTPException("search_uuid must be provided.")

    chat_history = []
    contexts = ""

    if search_uuid:
        if _app.ctx.should_do_chat_history:
            history = []
            try:
                history = await _app.loop.run_in_executor(
                    _app.ctx.executor, lambda sid: _app.ctx.kv.get(sid), f"{search_uuid}_history"
                )
                result = await _app.loop.run_in_executor(
                    _app.ctx.executor, lambda sid: _app.ctx.kv.get(sid), search_uuid
                )
            except KeyError:
                logger.info(f"Key {search_uuid} not found, will generate again.")
            except Exception as e:
                logger.error(f"KV error: {e}\n{traceback.format_exc()}, will generate again.")
            if history:
                last_entry = history[-1]
                old_query = last_entry.get("query", "")
                search_results = last_entry.get("search_results", "")
                llm_response_hist = last_entry.get("llm_response", "")
                if old_query and search_results:
                    if old_query != query:
                        contexts = history[-1]["search_results"]
                        chat_history = []
                        for entry in history:
                            if "query" in entry and "llm_response" in entry:
                                chat_history.append({"role": "user", "content": entry["query"]})
                                chat_history.append({"role": "assistant", "content": entry["llm_response"]})
                    else:
                        return sanic.text(result["txt"])
        else:
            try:
                result = await _app.loop.run_in_executor(
                    _app.ctx.executor, lambda sid: _app.ctx.kv.get(sid), search_uuid
                )
                if isinstance(result, dict):
                    if result["query"] == query:
                        return sanic.text(result["txt"])
            except KeyError:
                logger.info(f"Key {search_uuid} not found, will generate again.")
            except Exception as e:
                logger.error(f"KV error: {e}\n{traceback.format_exc()}, will generate again.")

    # Basic guard
    query = re.sub(r"\[/?INST\]", "", query)

    # If not using history or no cached contexts, fetch fresh
    if not _app.ctx.should_do_chat_history or contexts in ("", None):
        contexts = await _app.loop.run_in_executor(_app.ctx.executor, _app.ctx.search_function, query)

    system_prompt = _rag_query_text.format(
        context="\n\n".join([f"[[citation:{i+1}]] {c['snippet']}" for i, c in enumerate(contexts)])
    )

    try:
        related_questions_future = None
        if _app.ctx.should_do_related_questions and generate_related_questions:
            related_questions_future = get_related_questions(_app, query, contexts)

        if "claude-3" in _app.ctx.model.lower():
            logger.info("Using Claude for generating LLM response")
            client = new_async_client(_app)
            messages = []
            if chat_history:
                messages.extend(chat_history)
            messages.append({"role": "user", "content": query})

            response = await request.respond(content_type="text/html")
            all_yielded_results = []

            # Send contexts + marker and publish via Fanout
            context_str = json.dumps(contexts)
            await response.send(context_str)
            all_yielded_results.append(context_str)
            fanout_publish(search_uuid, sse_event("sources", contexts))

            await response.send("\n\n__LLM_RESPONSE__\n\n")
            all_yielded_results.append("\n\n__LLM_RESPONSE__\n\n")
            fanout_publish(search_uuid, sse_event("marker", {"t": "__LLM_RESPONSE__"}))

            if not contexts:
                warning = "(The search engine returned nothing for this query. Please take the answer with a grain of salt.)\n\n"
                await response.send(warning)
                all_yielded_results.append(warning)
                fanout_publish(search_uuid, sse_event("delta", {"text": warning}))

            if related_questions_future is not None:
                related_questions_task = asyncio.create_task(related_questions_future)

            async with client.messages.stream(
                model=_app.ctx.model,
                max_tokens=1024,
                system=system_prompt,
                messages=messages
            ) as stream:
                async for text_part in stream.text_stream:
                    if text_part:
                        all_yielded_results.append(text_part)
                        await response.send(text_part)
                        fanout_publish(search_uuid, sse_event("delta", {"text": text_part}))

            logger.info("Finished streaming LLM response")

            if related_questions_future is not None:
                try:
                    related_questions = await related_questions_task
                    result_json = json.dumps(related_questions)
                    await response.send("\n\n__RELATED_QUESTIONS__\n\n")
                    all_yielded_results.append("\n\n__RELATED_QUESTIONS__\n\n")
                    await response.send(result_json)
                    all_yielded_results.append(result_json)
                    fanout_publish(search_uuid, sse_event("related", related_questions))
                except Exception as e:
                    logger.error(f"Error during related questions generation: {e}")

            # Done
            fanout_publish(search_uuid, sse_event("done", {}))

        else:
            logger.info("Using OpenAI for generating LLM response")
            openai_client = new_async_client(_app)
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": query},
            ]
            if chat_history and len(chat_history) % 2 == 0:
                messages[1:1] = chat_history

            llm_response = await openai_client.chat.completions.create(
                model=_app.ctx.model,
                messages=messages,
                max_tokens=1024,
                stream=True,
                temperature=0.9,
            )

            response = await request.respond(content_type="text/html")
            all_yielded_results = []

            # Reuse unified streamer that also publishes to Fanout
            async for result_chunk in _raw_stream_response(
                _app, search_uuid, contexts, llm_response, related_questions_future
            ):
                all_yielded_results.append(result_chunk)
                await response.send(result_chunk)
            logger.info("Finished streaming LLM response")

    except Exception as e:
        logger.error(f"encountered error: {e}\n{traceback.format_exc()}")
        return sanic.json({"message": "Internal server error."}, 503)

    await response.eof()

    # Persist to KV (non-blocking)
    if _app.ctx.should_do_chat_history:
        _search_results, _llm_response, _related_questions = await _app.loop.run_in_executor(
            _app.ctx.executor, extract_all_sections, "".join(all_yielded_results)
        )
        if _search_results:
            _search_results = json.loads(_search_results)
        if _related_questions:
            _related_questions = json.loads(_related_questions)
        _ = _app.ctx.executor.submit(
            _app.ctx.kv.append, f"{search_uuid}_history", {
                "query": query,
                "search_results": _search_results,
                "llm_response": _llm_response,
                "related_questions": _related_questions
            }
        )
    _ = _app.ctx.executor.submit(
        _app.ctx.kv.put, search_uuid, {"query": query, "txt": "".join(all_yielded_results)}
    )

app.static("/ui", os.path.join(BASE_DIR, "ui/"), name="/")
app.static("/", os.path.join(BASE_DIR, "ui/index.html"), name="ui")

if __name__ == "__main__":
    port = int(os.getenv("PORT") or 8800)
    workers = int(os.getenv("WORKERS") or 1)
    app.run(host="0.0.0.0", port=port, workers=workers, debug=False)
