import re
import os
import regex
import json
import time
import spacy
from datetime import datetime
from Agent import Agent
from Prompts import Prompts
from extensions.searxng import searxng
from urllib.parse import urlparse
import logging
from concurrent.futures import Future


class Interactions:
    def __init__(self, agent_name: str = "AGiXT"):
        self.agent_name = agent_name
        self.agent = Agent(self.agent_name)
        self.agent_commands = self.agent.get_commands_string()
        self.stop_running_event = None
        self.browsed_links = []
        self.failures = 0
        self.nlp = None

    def load_spacy_model(self):
        if not self.nlp:
            try:
                self.nlp = spacy.load("en_core_web_sm")
            except:
                spacy.cli.download("en_core_web_sm")
                self.nlp = spacy.load("en_core_web_sm")
        self.nlp.max_length = 99999999999999999999999

    def custom_format(self, string, **kwargs):
        if isinstance(string, list):
            string = "".join(str(x) for x in string)

        def replace(match):
            key = match.group(1)
            value = kwargs.get(key, match.group(0))
            if isinstance(value, list):
                return "".join(str(x) for x in value)
            else:
                return str(value)

        pattern = r"(?<!{){([^{}\n]+)}(?!})"
        result = re.sub(pattern, replace, string)
        return result

    def get_step_response(self, chain_name, step_number):
        base_path = os.path.join(os.getcwd(), "chains")
        file_path = os.path.normpath(
            os.path.join(base_path, chain_name, "responses.json")
        )
        if not file_path.startswith(base_path):
            raise ValueError("Invalid path, chain name must not contain slashes.")
        try:
            with open(file_path, "r") as f:
                responses = json.load(f)
            return responses.get(str(step_number))
        except:
            return ""

    def get_step_content(self, chain_name, step_number, prompt_content):
        new_prompt_content = {}
        for arg, value in prompt_content.items():
            if "{STEP" in value:
                # get the response from the step number
                step_response = self.get_step_response(
                    chain_name=chain_name, step_number=step_number
                )
                # replace the {STEPx} with the response
                value = value.replace(f"{{STEP{step_number}}}", step_response)
            new_prompt_content[arg] = value
        return new_prompt_content

    async def format_prompt(
        self,
        user_input: str = "",
        top_results: int = 5,
        prompt="",
        chain_name="",
        step_number=0,
        memories=None,
        **kwargs,
    ):
        cp = Prompts()
        if prompt == "":
            prompt = user_input
        else:
            try:
                prompt = cp.get_prompt(prompt_name=prompt, model=self.agent.AI_MODEL)
            except:
                prompt = prompt
        if top_results == 0:
            context = "None"
        else:
            try:
                context = await memories.context_agent(
                    query=user_input, top_results_num=top_results
                )
            except:
                context = "None."
        command_list = self.agent.get_commands_string()
        if chain_name != "":
            try:
                for arg, value in kwargs.items():
                    if "{STEP" in value:
                        # get the response from the step number
                        step_response = self.get_step_response(
                            chain_name=chain_name, step_number=step_number
                        )
                        # replace the {STEPx} with the response
                        value = value.replace(f"{{STEP{step_number}}}", step_response)
                        kwargs[arg] = value
            except:
                logging.info("No args to replace.")
            if "{STEP" in prompt:
                step_response = self.get_step_response(
                    chain_name=chain_name, step_number=step_number
                )
                prompt = prompt.replace(f"{{STEP{step_number}}}", step_response)
            if "{STEP" in user_input:
                step_response = self.get_step_response(
                    chain_name=chain_name, step_number=step_number
                )
                user_input = user_input.replace(f"{{STEP{step_number}}}", step_response)
        formatted_prompt = self.custom_format(
            string=prompt,
            user_input=user_input,
            agent_name=self.agent_name,
            COMMANDS=self.agent_commands,
            context=context,
            command_list=command_list,
            date=datetime.now().strftime("%B %d, %Y %I:%M %p"),
            **kwargs,
        )

        if not self.nlp:
            self.load_spacy_model()
        tokens = len(self.nlp(formatted_prompt))
        logging.info(f"FORMATTED PROMPT: {formatted_prompt}")
        return formatted_prompt, prompt, tokens

    async def run(
        self,
        user_input: str = "",
        prompt: str = "",
        context_results: int = 5,
        websearch: bool = False,
        websearch_depth: int = 3,
        learn_file: str = "",
        chain_name: str = "",
        step_number: int = 0,
        **kwargs,
    ):
        logging.info(f"KWARGS: {kwargs}")
        memories = self.agent.get_memories()
        if learn_file != "":
            learning_file = await memories.mem_read_file(file_path=learn_file)
            if learning_file == False:
                return "Failed to read file."
        formatted_prompt, unformatted_prompt, tokens = await self.format_prompt(
            user_input=user_input,
            top_results=context_results,
            prompt=prompt,
            chain_name=chain_name,
            step_number=step_number,
            memories=memories,
            **kwargs,
        )
        if websearch:
            await self.websearch_agent(user_input=user_input, depth=websearch_depth)
        try:
            # Workaround for non-threaded providers
            run_response = await self.agent.instruct(formatted_prompt, tokens=tokens)
            self.response = (
                run_response.result()
                if isinstance(run_response, Future)
                else run_response
            )
        except Exception as e:
            logging.info(f"Error: {e}")
            logging.info(f"PROMPT CONTENT: {formatted_prompt}")
            logging.info(f"TOKENS: {tokens}")
            self.failures += 1
            if self.failures == 5:
                self.failures == 0
                logging.info("Failed to get a response 5 times in a row.")
                return None
            logging.info(f"Retrying in 10 seconds...")
            time.sleep(10)
            if context_results > 0:
                context_results = context_results - 1
            self.response = await self.run(
                user_input=user_input,
                prompt=prompt,
                context_results=context_results,
                **kwargs,
            )

        # Handle commands if the prompt contains the {COMMANDS} placeholder
        # We handle command injection that DOESN'T allow command execution by using {command_list} in the prompt
        if "{COMMANDS}" in unformatted_prompt:
            execution_response = await self.execution_agent(
                execution_response=self.response,
                user_input=user_input,
                context_results=context_results,
                **kwargs,
            )
            return_response = ""
            try:
                self.response = json.loads(self.response)
                if "response" in self.response:
                    return_response = self.response["response"]
                if "commands" in self.response:
                    if self.response["commands"] != {}:
                        return_response += (
                            f"\n\nCommands Executed:\n{self.response['commands']}"
                        )
                if execution_response:
                    return_response += (
                        f"\n\nCommand Execution Response:\n{execution_response}"
                    )
            except:
                return_response = self.response
            self.response = return_response
        logging.info(f"Response: {self.response}")
        if self.response != "" and self.response != None:
            try:
                await memories.store_result(input=user_input, result=self.response)
            except:
                pass
            self.agent.log_interaction(role="USER", message=user_input)
            self.agent.log_interaction(role=self.agent_name, message=self.response)
        return self.response

    async def smart_instruct(
        self,
        user_input: str = "Write a tweet about AI.",
        shots: int = 3,
        learn_file: str = "",
        objective: str = None,
        **kwargs,
    ):
        answers = []
        # Do multi shots of prompt to get N different answers to be validated
        answers.append(
            await self.run(
                user_input=user_input,
                prompt="SmartInstruct-StepByStep"
                if objective == None
                else "SmartTask-StepByStep",
                context_results=6,
                websearch=True,
                websearch_depth=3,
                shots=shots,
                learn_file=learn_file,
                objective=objective,
                **kwargs,
            )
        )
        if shots > 1:
            for i in range(shots - 1):
                answers.append(
                    await self.run(
                        user_input=user_input,
                        prompt="SmartInstruct-StepByStep"
                        if objective == None
                        else "SmartTask-StepByStep",
                        context_results=6,
                        shots=shots,
                        objective=objective,
                        **kwargs,
                    )
                )
        answer_str = ""
        for i, answer in enumerate(answers):
            answer_str += f"Answer {i + 1}:\n{answer}\n\n"
        researcher = await self.run(
            user_input=answer_str,
            prompt="SmartInstruct-Researcher",
            shots=shots,
            **kwargs,
        )
        resolver = await self.run(
            user_input=researcher,
            prompt="SmartInstruct-Resolver",
            shots=shots,
            **kwargs,
        )
        execution_response = await self.run(
            user_input=f"{user_input}\nContext:\n{resolver}",
            prompt="instruct",
            **kwargs,
        )
        response = f"{resolver}\n\n{execution_response}"
        return response

    async def smart_chat(
        self,
        user_input: str = "Write a tweet about AI.",
        shots: int = 3,
        learn_file: str = "",
        **kwargs,
    ):
        answers = []
        answers.append(
            await self.run(
                user_input=user_input,
                prompt="SmartChat-StepByStep",
                context_results=6,
                websearch=True,
                websearch_depth=3,
                shots=shots,
                learn_file=learn_file,
                **kwargs,
            )
        )
        # Do multi shots of prompt to get N different answers to be validated
        if shots > 1:
            for i in range(shots - 1):
                answers.append(
                    await self.run(
                        user_input=user_input,
                        prompt="SmartChat-StepByStep",
                        context_results=6,
                        shots=shots,
                        **kwargs,
                    )
                )
        answer_str = ""
        for i, answer in enumerate(answers):
            answer_str += f"Answer {i + 1}:\n{answer}\n\n"
        researcher = await self.run(
            user_input=answer_str,
            prompt="SmartChat-Researcher",
            context_results=6,
            shots=shots,
            **kwargs,
        )
        resolver = await self.run(
            user_input=researcher,
            prompt="SmartChat-Resolver",
            context_results=6,
            shots=shots,
            **kwargs,
        )
        return resolver

    # Worker Sub-Agents
    async def validation_agent(
        self, user_input, execution_response, context_results, **kwargs
    ):
        try:
            pattern = regex.compile(r"\{(?:[^{}]|(?R))*\}")
            cleaned_json = pattern.findall(execution_response)
            if len(cleaned_json) == 0:
                return {}
            if isinstance(cleaned_json, list):
                cleaned_json = cleaned_json[0]
            response = json.loads(cleaned_json)
            return response
        except:
            logging.info("INVALID JSON RESPONSE")
            logging.info(execution_response)
            logging.info("... Trying again.")
            if context_results != 0:
                context_results = context_results - 1
            else:
                context_results = 0
            execution_response = await self.run(
                user_input=user_input, context_results=context_results, **kwargs
            )
            return await self.validation_agent(
                user_input=user_input,
                execution_response=execution_response,
                context_results=context_results,
                **kwargs,
            )

    async def execution_agent(
        self, execution_response, user_input, context_results, **kwargs
    ):
        validated_response = await self.validation_agent(
            user_input=user_input,
            execution_response=execution_response,
            context_results=context_results,
            **kwargs,
        )
        if "commands" in validated_response:
            for command_name, command_args in validated_response["commands"].items():
                # Search for the command in the available_commands list, and if found, use the command's name attribute for execution
                if command_name is not None:
                    for available_command in self.agent.available_commands:
                        if command_name == available_command["friendly_name"]:
                            # Check if the command is a valid command in the self.avent.available_commands list
                            try:
                                command_output = await self.agent.execute(
                                    command_name=command_name, command_args=command_args
                                )
                            except Exception as e:
                                logging.info("Command validation failed, retrying...")
                                validate_command = await self.run(
                                    user_input=user_input,
                                    prompt="ValidationFailed",
                                    command_name=command_name,
                                    command_args=command_args,
                                    command_output=e,
                                    context_results=context_results,
                                    **kwargs,
                                )
                                return await self.execution_agent(
                                    execution_response=validate_command,
                                    user_input=user_input,
                                    context_results=context_results,
                                    **kwargs,
                                )
                            logging.info(
                                f"Command {command_name} executed successfully with args {command_args}. Command Output: {command_output}"
                            )
                            response = f"\nExecuted Command:{command_name} with args {command_args}.\nCommand Output: {command_output}\n"
                            return response
                else:
                    if command_name == "None.":
                        return "\nNo commands were executed.\n"
                    else:
                        return f"\Command not recognized: `{command_name}`."
        else:
            return "\nNo commands were executed.\n"

    async def websearch_agent(
        self,
        user_input: str = "What are the latest breakthroughs in AI?",
        depth: int = 3,
    ):
        memories = self.agent.get_memories()

        async def resursive_browsing(user_input, links):
            try:
                words = links.split()
                links = [
                    word for word in words if urlparse(word).scheme in ["http", "https"]
                ]
            except:
                links = links
            if links is not None:
                for link in links:
                    if "href" in link:
                        try:
                            url = link["href"]
                        except:
                            url = link
                    else:
                        url = link
                    url = re.sub(r"^.*?(http)", r"http", url)
                    # Check if url is an actual url
                    if url.startswith("http"):
                        logging.info(f"Scraping: {url}")
                        if url not in self.browsed_links:
                            self.browsed_links.append(url)
                            (
                                collected_data,
                                link_list,
                            ) = await memories.read_website(url)
                            if link_list is not None:
                                if len(link_list) > 0:
                                    if len(link_list) > 5:
                                        link_list = link_list[:3]
                                    try:
                                        pick_a_link = await self.run(
                                            user_input=user_input,
                                            prompt="Pick-a-Link",
                                            links=link_list,
                                        )
                                        if not pick_a_link.startswith("None"):
                                            await resursive_browsing(
                                                user_input, pick_a_link
                                            )
                                    except:
                                        logging.info(
                                            f"Issues reading {url}. Moving on..."
                                        )

        results = await self.run(user_input=user_input, prompt="WebSearch")
        results = results.split("\n")
        for result in results:
            search_string = result.lstrip("0123456789. ")
            try:
                searx_server = self.agent.PROVIDER_SETTINGS["SEARXNG_INSTANCE_URL"]
            except:
                searx_server = ""
            try:
                links = await searxng(SEARXNG_INSTANCE_URL=searx_server).search(
                    search_string
                )
                if len(links) > depth:
                    links = links[:depth]
            except:
                links = None
            if links is not None:
                await resursive_browsing(user_input, links)
