import os

from comfy_api.latest import ComfyExtension, io

from .nodes.gen_text_node import GenerateText


class VeniceExtension(ComfyExtension):
    name = "comfyui_venice_api_v3"

    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return [
            GenerateText,
        ]


async def comfy_entrypoint() -> VeniceExtension:
    return VeniceExtension()


WEB_DIRECTORY = os.path.join(os.path.dirname(__file__), "js")

__all__ = ["WEB_DIRECTORY"]
