# Standard library imports
from typing import Any

# Third-party imports
from ollama import chat

# Local application imports
from config.config import Config
from src.templates.template_manager import TemplateManager


class ResponseEngine:
    """
    A class for generating responses using a specified model or strategy.
    """

    def __init__(self, model_name: str) -> None:
        """
        Initialize the response engine with a specific model type.

        Args:
            model_name (str): Identifier for the type of model or response generation strategy to use.
        """
        self._model = model_name

    def _load_model(self, model_name: str) -> Any:
        """
        Load a model based on the specified model type.

        Args:
            model_name (str): The type of model to load.

        Returns:
            Any: The loaded model.
        """
        self._model = model_name

    def generate_response(self, llm_prompt: str) -> str:
        """
        Generate a response based on the enriched query (llm_prompt) using the loaded model or strategy.

        Args:
            llm_prompt (str): The enriched query that combines the user prompt with context from recent messages and long-term memory.

        Returns:
            str: The generated response.
        """
        messages = [
            {
                "role": "system",
                "content": TemplateManager.get("system_message", model=self._model),
            },
            {
                "role": "user",
                "content": llm_prompt,
            },
        ]

        # Send the messages to the chat model
        response = chat(
            Config.get("default_inference_model"), messages=messages, stream=False
        )

        return response["message"]["content"]
