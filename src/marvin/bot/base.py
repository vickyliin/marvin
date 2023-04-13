import asyncio
import functools
import inspect
import json
import re
from typing import TYPE_CHECKING, Any, Callable

import pendulum
from fastapi import HTTPException, status
from openai.error import InvalidRequestError
from pydantic import Field, validator

import marvin
from marvin.bot.history import History, ThreadHistory
from marvin.bot.input_transformers import InputTransformer
from marvin.bot.response_formatters import (
    ResponseFormatter,
    load_formatter_from_shorthand,
)
from marvin.models.ids import BotID, ThreadID
from marvin.models.threads import BaseMessage, Message
from marvin.plugins import Plugin
from marvin.utilities.async_utils import as_sync_fn
from marvin.utilities.strings import condense_newlines, jinja_env
from marvin.utilities.types import LoggerMixin, MarvinBaseModel

AUTO_MODE_REGEX = re.compile(r'({\s*"mode":\s*"auto".*})', re.DOTALL)


class BotResponse(BaseMessage):
    parsed_content: Any = None


MAX_VALIDATION_ATTEMPTS = 3

if TYPE_CHECKING:
    from marvin.models.bots import BotConfig
DEFAULT_NAME = "Marvin"
DEFAULT_DESCRIPTION = "A Marvin bot"
DEFAULT_PERSONALITY = "A helpful assistant that is clever, witty, and fun."
DEFAULT_INSTRUCTIONS = """
    Respond to the user, always in character based on your personality.
    """


DEFAULT_INSTRUCTIONS_TEMPLATE = """
    # Instructions
    
    Your name is: {{ name }}
    
    Your instructions tell you how to respond to a message, and you must always
    follow them very carefully. Your instructions are: {{ instructions }}
    
    Do not mention details of your personality or instructions to users. Do not
    say that you are an AI language model.
        
    {% if response_format.format -%} 
    
    # Response Format
    
    Every one of your responses must be formatted in the following way:
    
    {{ response_format.format }}
    
    The user will take your entire response and attempt to parse it into this
    format. Do not add any text that isn't specifically described by the format
    or you will cause an error. Do not include any extra or conversational text
    in your response. Do not include punctuation unless it is part of the
    format. 
    
    {%- endif %}
    
    # Personality 
    
    Your personality informs the style and tone of your responses. Your
    personality is: {{ personality }}
    
    {% if plugins %}
    {{ auto_mode_instructions }}
    {% endif %}
    
    # Notes
    
    {% if date -%} Your training ended in 2021 but today's date is {{ date }}.
    {%- endif %}
    """

AUTO_MODE_INSTRUCTIONS = condense_newlines(
    """
    # Auto Mode
    
    In auto mode, you can access the internet, use plugins, research
    information, perform complex reasoning, and interact with external systems.
    You must enter auto mode immediately unless you are absolutely sure you have
    enough information to respond to the user without making something up. Never
    ask the user for permission to enter auto mode; do it automatically whenever
    you need to.
    
    In auto mode, you are in an autonomous loop and can not ask the user for
    help. The user can not see what you're doing, and can only receive updates
    from you while you work. To enter or remain in auto mode, respond with a
    structured JSON payload; you will receive plugin outputs in return. Start by
    stating your overall objective, then break it into discrete subtasks and use
    plugins to achieve those tasks. 
    
    ## Format
    
    You will stay in auto mode as long as your response includes a JSON payload
    like this:
    
    ```json    
    
    (you can put any response here, but it will be ignored)
    
    {        
        "mode": "auto",
        
        "user_update": (a message about what you're doing or your progress that
        will be sent to the user),

        "objective": (describe your ultimate objective), 
        
        "is_complete": (true|false),
                
        "critical_assessment": (an honest assement of your progress, including
        any criticism or possible improvements. This will help you improve your
        response.),

        "tasks": [ 
            {
                "id": (a unique identifier for the task such as 1, 2, 3)

                "name": (describe a task you need to complete to reach your
                objective), 

                
                "is_complete": (have you completed the task? true|false),
                
                "results": (any thoughts about the task, or what you learned
                from completing it. DO NOT MAKE ANYTHING UP.)
            },
            ...
        ],
        
        "plugins": [
            {
                "name": (MUST be one of [{{ plugins|join(', ',
                attribute='name')}}])
                
                "inputs": {arg: value}
                
                "tasks": [(a list of any task ids this is related to. This is
                used to extract relevant information from the plugin output.)]
            }, 
            ...
        ]
    } 
        
    ```
    
    The `tasks` section tracks your progress toward a solution. You can update
    it at any time. Your tasks should reflect each step you need to take to
    deliver your ultimate objective. Tasks should always start with
    `is_complete=false`. Do not mark the objective complete until all tasks are
    complete. Auto mode will exit when the objective is complete. After you say
    a task is complete for the first time, you don't need to include it in the
    payload anymore.
        
    The `plugins` section instructs the system to call plugins and return the
    results to you as a system message. You can use as many plugins as you want
    at a time. Only include plugins you want to call on the next loop. Do not
    include plugins for completed tasks.
    
    You have the following plugins available: 
    
    {% for plugin in plugins %} - {{ plugin.get_full_description() }}
    
    {% endfor -%}
    
    After each loop, you will be provided with any plugin outputs that you
    requested, as well as your previous JSON response. To exit auto mode, simply
    respond normally without a JSON payload.
"""
)


DEFAULT_PLUGINS = [
    marvin.plugins.web.VisitURL(),
    marvin.plugins.duckduckgo.DuckDuckGo(),
    marvin.plugins.mathematics.Calculator(),
]


class Bot(MarvinBaseModel, LoggerMixin):
    class Config:
        validate_assignment = True

    id: BotID = Field(default_factory=BotID.new)
    name: str = Field(None, description='The name of the bot. Defaults to "Marvin".')
    description: str = Field(
        None,
        description=(
            "A optional description of the bot. This is for documentation only and will"
            " NOT be shown to the bot."
        ),
    )
    personality: str = Field(None, description="The bot's personality.", repr=False)
    instructions: str = Field(
        None,
        description="Instructions for the bot to follow when responding.",
        repr=False,
    )
    plugins: list[Plugin] = Field(
        None, description="A list of plugins that the bot can use."
    )
    history: History = Field(None, repr=False)
    llm_model_name: str = Field(
        default_factory=lambda: marvin.settings.openai_model_name, repr=False
    )
    llm_model_temperature: float = Field(
        default_factory=lambda: marvin.settings.openai_model_temperature, repr=False
    )

    input_transformers: list[InputTransformer] = Field(
        default_factory=list,
        description=(
            "A list of input transformers to apply to the user input before passing it"
            ' to the LLM. For example, you can use the "PrependText" input transformer'
            " to prepend the user input with a string."
        ),
        repr=False,
    )
    input_prompt: str = Field(
        "{0}",
        description=(
            "A template for the input to the bot. This allows users to specify how the"
            " bot should combine multiple inputs. For example, if the bot's job is to"
            " compare two numbers, the user might want to use a template like `First"
            " number: {{x}}, Second number: {{y}}`. The user could invoke the bot as"
            " `bot.say(x=3, y=4)`"
        ),
        repr=False,
    )
    response_format: ResponseFormatter = Field(
        None,
        description=(
            "A description of how the bot should format its response. This could be a"
            " literal type, a complex data structure or Pydantic model, a string"
            " template, a regex, or a natural language description. For more complete"
            " control of validation, pass an `ResponseFormatter` object; otherwise one"
            " will be inferred based on your input."
        ),
        repr=False,
    )
    include_date_in_prompt: bool = Field(
        True,
        description="Include the date in the prompt. Disable for testing.",
        repr=False,
    )

    instructions_template: str = Field(
        None,
        description="A template for the instructions that the bot will receive.",
        repr=False,
    )

    @validator("name", always=True)
    def handle_name(cls, v):
        if v is None:
            v = DEFAULT_NAME
        return condense_newlines(v)

    @validator("description", always=True)
    def handle_description(cls, v):
        if v is None:
            return DEFAULT_DESCRIPTION
        return condense_newlines(v)

    @validator("personality", always=True)
    def handle_personality(cls, v):
        if v is None:
            v = DEFAULT_PERSONALITY
        return condense_newlines(v)

    @validator("instructions", always=True)
    def handle_instructions(cls, v):
        if v is None:
            v = DEFAULT_INSTRUCTIONS
        return condense_newlines(v)

    @validator("instructions_template", always=True)
    def handle_instructions_template(cls, v):
        if v is None:
            v = DEFAULT_INSTRUCTIONS_TEMPLATE
        return condense_newlines(v)

    @validator("plugins", always=True)
    def default_plugins(cls, v):
        if v is None:
            if marvin.settings.bot_load_default_plugins:
                return DEFAULT_PLUGINS
            else:
                return []
        return v

    @validator("history", always=True)
    def default_history(cls, v):
        if v is None:
            return ThreadHistory()
        return v

    @validator("response_format", pre=True, always=True)
    def response_format_to_string(cls, v):
        if v is None:
            return ResponseFormatter()
        elif isinstance(v, ResponseFormatter):
            return v
        else:
            return marvin.bot.response_formatters.load_formatter_from_shorthand(v)

    def to_bot_config(self) -> "BotConfig":
        from marvin.models.bots import BotConfig

        return BotConfig(
            id=self.id,
            name=self.name,
            personality=self.personality,
            instructions=self.instructions,
            instructions_template=self.instructions_template,
            plugins=[p.dict() for p in self.plugins],
            input_transformers=[t.dict() for t in self.input_transformers],
            description=self.description,
        )

    @classmethod
    def from_bot_config(cls, bot_config: "BotConfig") -> "Bot":
        return cls(
            id=bot_config.id,
            name=bot_config.name,
            personality=bot_config.personality,
            instructions=bot_config.instructions,
            instructions_template=bot_config.instructions_template,
            plugins=bot_config.plugins,
            input_transformers=bot_config.input_transformers,
            description=bot_config.description,
        )

    async def save(self, if_exists: str = None):
        """
        Save this bot in the database. Bots are saved by name. By default,
        errors if a bot with the same name exists. Customize this behavior with
        the `if_exists` parameter, which can be `delete`, `update`, or `cancel`:
            - `delete`: Delete the existing bot and save this one. The bots will
              not have the same ID.
            - `update`: Update the existing bot with the values from this one.
              The bots will have the same ID and message history.
            - `cancel`: Do nothing (and do not raise an error).
        """
        if if_exists not in [None, "delete", "update", "cancel"]:
            raise ValueError(
                "if_exists must be one of 'delete', 'update', or 'cancel'. Got"
                f" {if_exists}"
            )

        bot_config = self.to_bot_config()
        try:
            # this will error if there's no bot
            await marvin.api.bots.get_bot_config(name=self.name)

            # if no error, then the bot exists
            if not if_exists:
                raise ValueError(f"Bot with name {self.name} already exists.")
            elif if_exists == "delete":
                await marvin.api.bots.delete_bot_config(name=self.name)
                await marvin.api.bots.create_bot_config(bot_config=bot_config)
                return
            elif if_exists == "update":
                await marvin.api.bots.update_bot_config(
                    name=self.name,
                    bot_config=marvin.models.bots.BotConfigUpdate(**bot_config.dict()),
                )
                return
            elif if_exists == "cancel":
                return
        except HTTPException as exc:
            if exc.status_code == status.HTTP_404_NOT_FOUND:
                pass
            else:
                raise

        # create the bot
        await marvin.api.bots.create_bot_config(bot_config=bot_config)

    @classmethod
    async def load(cls, name: str) -> "Bot":
        """Load a bot from the database."""
        bot_config = await marvin.api.bots.get_bot_config(name=name)
        return cls.from_bot_config(bot_config=bot_config)

    async def say(
        self, *args, response_format=None, on_token_callback: Callable = None, **kwargs
    ) -> BotResponse:
        # process inputs
        message = self.input_prompt.format(*args, **kwargs)

        # get bot instructions
        bot_instructions = await self._get_bot_instructions(
            response_format=response_format
        )

        # load chat history
        history = await self._get_history()

        # apply input transformers
        for t in self.input_transformers:
            message = t.run(message)
            if inspect.iscoroutine(message):
                message = await message
        user_message = Message(role="user", name="User", content=message)

        messages = [bot_instructions] + history + [user_message]

        self.logger.debug_kv("User message", message, "bold blue")
        await self.history.add_message(user_message)

        counter = 1
        auto_mode_json = ""
        auto_mode_messages = []
        while counter <= marvin.settings.bot_max_iterations:
            counter += 1
            response = await self._call_langchain_llm(
                messages=messages + auto_mode_messages,
                on_token_callback=on_token_callback,
            )

            if not self.plugins:
                return response

            auto_mode_messages, auto_mode_json = await self._parse_auto_mode_payload(
                response
            )

            if not auto_mode_messages:
                break

        # validate response format
        parsed_response = response
        validated = False

        for _ in range(MAX_VALIDATION_ATTEMPTS):
            try:
                self.response_format.validate_response(response)
                validated = True
                break
            except Exception as exc:
                on_error = self.response_format.on_error
                if on_error == "ignore":
                    break
                elif on_error == "raise":
                    raise exc
                elif on_error == "reformat":
                    self.logger.debug(
                        "Response did not pass validation. Attempted to reformat:"
                        f" {response}"
                    )
                    response = _reformat_response(
                        user_message=user_message.content,
                        ai_response=response,
                        error_message=repr(exc),
                        target_return_type=self.response_format.format,
                    )
                else:
                    raise ValueError(f"Unknown on_error value: {on_error}")
        else:
            response = (
                "Error: could not validate response after"
                f" {MAX_VALIDATION_ATTEMPTS} attempts."
            )
            parsed_response = response

        if validated:
            parsed_response = self.response_format.parse_response(response)

        ai_response = BotResponse(
            name=self.name,
            role="bot",
            content=response,
            parsed_content=parsed_response,
            bot_id=self.id,
        )
        await self.history.add_message(ai_response)
        self.logger.debug_kv("AI message", ai_response.content, "bold green")
        return ai_response

    async def reset_thread(self):
        await self.history.clear()

    async def set_thread(
        self, thread_id: ThreadID = None, thread_lookup_key: str = None
    ):
        if thread_id is None and thread_lookup_key is None:
            raise ValueError("Must provide either thread_id or thread_lookup_key")
        elif thread_id is not None and thread_lookup_key is not None:
            raise ValueError(
                "Must provide either thread_id or thread_lookup_key, not both"
            )
        elif thread_id:
            self.history = ThreadHistory(thread_id=thread_id)
        elif thread_lookup_key:
            thread = await marvin.api.threads.get_or_create_thread_by_lookup_key(
                lookup_key=thread_lookup_key
            )
            self.history = ThreadHistory(thread_id=thread.id)

    async def _get_bot_instructions(self, response_format=None) -> Message:
        if response_format is not None:
            response_format = load_formatter_from_shorthand(response_format)
        else:
            response_format = self.response_format

        jinja_instructions = jinja_env.from_string(self.instructions)
        jinja_auto_mode_instructions = jinja_env.from_string(AUTO_MODE_INSTRUCTIONS)

        # prepare instructions variables
        vars = dict(
            name=self.name,
            response_format=response_format,
            personality=self.personality,
            include_date=self.include_date_in_prompt,
            date=pendulum.now().format("dddd, MMMM D, YYYY"),
            plugins=self.plugins,
        )

        bot_instructions = jinja_env.from_string(self.instructions_template).render(
            **vars,
            instructions=jinja_instructions.render(**vars),
            auto_mode_instructions=jinja_auto_mode_instructions.render(**vars),
        )

        return Message(role="system", content=bot_instructions)

    async def _get_history(self) -> list[Message]:
        history = await self.history.get_messages(
            max_tokens=3500 - marvin.settings.openai_model_max_tokens
        )
        return history

    async def _call_langchain_llm(
        self, messages: list[Message], on_token_callback: Callable = None
    ) -> str:
        """
        Get an LLM response to a history of Marvin messages via langchain
        """

        # deferred import for performance

        import marvin.utilities.llms

        langchain_messages = marvin.utilities.llms.prepare_messages(messages)
        llm = marvin.utilities.llms.get_llm(
            model_name=self.llm_model_name,
            temperature=self.llm_model_temperature,
            on_token_callback=on_token_callback,
        )

        if marvin.settings.verbose:
            messages_repr = "\n".join(repr(m) for m in langchain_messages)
            self.logger.debug(f"Sending messages to LLM: {messages_repr}")
        try:
            result = await llm.agenerate(messages=[langchain_messages])
        except InvalidRequestError as exc:
            if "does not exist" in str(exc):
                raise ValueError(
                    "Please check your `openai_model_name` and that your OpenAI account"
                    " has access to this model. You can select an OpenAI model by"
                    " setting the `MARVIN_OPENAI_MODEL_NAME` env var."
                    " Read more about settings in the docs: https://www.askmarvin.ai/guide/introduction/configuration/#settings"  # noqa: E501
                )
            raise exc

        return result.generations[0][0].text

    async def interactive_chat(self, first_message: str = None):
        """
        Launch an interactive chat with the bot. Optionally provide a first message.
        """
        await marvin.bot.interactive_chat.chat(bot=self, first_message=first_message)

    # -------------------------------------
    # Synchronous convenience methods
    # -------------------------------------
    @functools.wraps(say)
    def say_sync(self, *args, **kwargs) -> BotResponse:
        """
        A synchronous version of `say`. This is useful for testing or including
        a bot in a synchronous framework.
        """
        return as_sync_fn(self.say)(*args, **kwargs)

    @functools.wraps(save)
    def save_sync(self, *args, **kwargs):
        """
        A synchronous version of `save`. This is useful for testing or including
        a bot in a synchronous framework.
        """
        return as_sync_fn(self.save)(*args, **kwargs)

    @classmethod
    @functools.wraps(load)
    def load_sync(cls, *args, **kwargs):
        """
        A synchronous version of `load`. This is useful for testing or including
        a bot in a synchronous framework.
        """
        return as_sync_fn(cls.load)(*args, **kwargs)

    async def _parse_auto_mode_payload(self, response: str) -> list[Message]:
        messages = []
        auto_mode_json = None

        if match := AUTO_MODE_REGEX.search(response):
            try:
                auto_mode_json = json.loads(match.group(1))
                messages.append(Message(role="bot", content=response))
                self.logger.debug_kv("Auto Mode payload", auto_mode_json)

                plugins = auto_mode_json.get("plugins", [])
                plugin_outputs = await asyncio.gather(
                    *[self._run_plugin(p["name"], p["inputs"]) for p in plugins]
                )
                plugin_payload = dict(zip([p["name"] for p in plugins], plugin_outputs))

                messages.append(
                    Message(role="system", content=json.dumps(plugin_payload))
                )

            except json.JSONDecodeError as exc:
                messages.append(
                    Message(
                        role="system",
                        content=f"Auto Mode payload was invalid JSON, try again: {exc}",
                    )
                )

            except Exception as exc:
                messages.append(
                    Message(
                        role="system",
                        content=f"Auto Mode encountered an error, try again: {exc}",
                    )
                )

        return messages, auto_mode_json

    async def _run_plugin(self, plugin_name: str, plugin_inputs: dict) -> str:
        plugin = next((p for p in self.plugins if p.name == plugin_name.strip()), None)
        if plugin is None:
            return f'Plugin "{plugin_name}" not found.'
        try:
            self.logger.debug_kv(f'Running plugin "{plugin_name}"', plugin_inputs)
            plugin_output = plugin.run(**plugin_inputs)
            if inspect.iscoroutine(plugin_output):
                plugin_output = await plugin_output
            self.logger.debug_kv("Plugin output", plugin_output)

            return plugin_output
        except Exception as exc:
            self.logger.error(
                f"Error running plugin {plugin_name} with inputs"
                f" {plugin_inputs}:\n\n{exc}"
            )
            return f"Plugin encountered an error. Try again? Error message: {exc}"


def _reformat_response(
    user_message: str,
    ai_response: str,
    target_return_type: Any,
    error_message: str,
) -> str:
    @marvin.ai_fn(
        plugins=[],
        bot_modifier=lambda bot: setattr(bot.response_format, "on_error", "ignore"),
    )
    def reformat_response(
        response: str, user_message: str, target_return_type: str, error_message: str
    ) -> str:
        """
        The `response` contains an answer to the `user_prompt`. However it could
        not be parsed into the correct return format (`target_return_type`). The
        associated error message was `error_message`.

        Extract the answer from the `response` and return it as a string that
        can be parsed correctly.
        """

    return reformat_response(
        response=ai_response,
        user_message=user_message,
        target_return_type=target_return_type,
        error_message=error_message,
    )
