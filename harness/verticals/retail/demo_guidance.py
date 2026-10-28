"""Reviewed starting questions for the shipped demo corpus only.

DEMO-GUIDANCE.md records the frame review and source attribution. Customer
workspaces never receive these example questions or company identities.
"""
from copy import deepcopy
from urllib.parse import unquote


ACRONI_SOURCE = ("https://commons.wikimedia.org/wiki/File:Proizvodnja_vro%C4%8De_valjane_"
                 "debele_plo%C4%8Devine_v_Acroni_SIJ_Jesenice.ogv")


def _questions(*prompts):
    return [{"label": prompt, "prompt": prompt} for prompt in prompts]


GUIDANCE = {
    "cafe": {
        "name": "Café", "industry": "Hospitality",
        "description": "Counter service, seating and group activity across six café settings.",
        "source_url": "https://dk-kim.github.io/CAFE/",
        "questions": _questions(
            "Show me how drinks are handed over at the counter.",
            "Where are people waiting near the counter?",
            "Compare how the seating areas are being used."),
    },
    "warehouse": {
        "name": "Warehouse", "industry": "Logistics",
        "description": "Four warehouse views covering people, storage racks, pallets and aisles.",
        "questions": _questions(
            "Show where people walk past stored pallets.",
            "Find boxes or pallets beside the aisles.",
            "Make a short safety review with supporting clips."),
    },
    "steel-process": {
        "name": "Factory", "industry": "Manufacturing",
        "description": "A steel-rolling process with roller beds, glowing steel and cooling stages.",
        "questions": _questions(
            "Show me the stages of the steel-rolling process.",
            "Find glowing steel moving along the rollers.",
            "Create a process summary with clips and timestamps."),
    },
    "retail": {
        "name": "Retail", "industry": "Retail",
        "description": "Shop entrances and corridor activity in a Lisbon shopping centre.",
        "source_url": "https://homepages.inf.ed.ac.uk/rbf/CAVIARDATA1/",
        "questions": _questions(
            "Show people stopping at the shop entrance.",
            "Find interactions near the shop entrance.",
            "Summarize how people move past and into the shop."),
    },
}


def demo_guidance(environment_id, clips):
    if environment_id != "demo":
        return {}
    available = {clip.get("dataset") for clip in clips}
    result = {key: deepcopy(value) for key, value in GUIDANCE.items() if key in available}
    # The company identity follows the actual source attribution, not a label.
    if "steel-process" in result and all(
        unquote(str(clip.get("source_reference", ""))) == unquote(ACRONI_SOURCE)
        for clip in clips if clip.get("dataset") == "steel-process"
    ):
        result["steel-process"].update(
            name="SIJ Acroni", company_url="https://www.acroni.si/en/about-us",
            brand_image="/assets/demo-companies/sij-acroni.png",
            source_url=ACRONI_SOURCE,
            description="Steel rolling at SIJ Acroni in Jesenice, Slovenia.")
    return result
