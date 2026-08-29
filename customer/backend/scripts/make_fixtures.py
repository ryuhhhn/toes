"""Generate the fixture catalogs.

Two catalogs that are unlike each other on every axis that matters, per BUILD_PLAN Stage I1.
The mess is deliberate and each kind of mess is load-bearing for a test:

    power_tools.csv          .csv, UTF-8 BOM, snake_case headers, $1,299.00 currency,
                             unit-bearing numerics (18 V / 1.2 kg / 13mm), variant brand
                             spellings, assorted null tokens, out-of-stock rows,
                             one unparseable price.

    tea_and_infusions.xlsx   .xlsx, multi-sheet, Title Case With Spaces headers,
                             1.299,00 € currency convention, list-valued cells with two
                             different delimiters, two junk rows above the header, an
                             unnamed mostly-empty column, out-of-stock rows.

Run: uv run python scripts/make_fixtures.py
"""

from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd

OUT = Path(__file__).resolve().parent.parent / "fixtures" / "catalogs"

# --- Catalog A -------------------------------------------------------------------------

POWER_TOOL_HEADER = [
    "sku",
    "product_name",
    "brand",
    "tool_type",
    "power_source",
    "voltage",
    "weight",
    "chuck_size",
    "battery_included",
    "price_usd",
    "qty_on_hand",
    "image_url",
    "description",
    "warranty_years",
]

POWER_TOOLS = [
    ["PT-1001", "Compact Drill Driver 18V", "DeWalt", "drill", "cordless", "18 V", "1.2 kg", "13mm", "Yes", "$149.00", "14", "https://img.example.com/pt1001.jpg", "Light two-speed drill for cabinet work and general assembly.", "3"],
    ["PT-1002", "Heavy Duty Hammer Drill", "Dewalt", "hammer drill", "corded", "230 V", "3.4 kg", "13 mm", "No", "$219.50", "6", "https://img.example.com/pt1002.jpg", "Concrete-capable percussion drill with depth stop and side handle.", "3"],
    ["PT-1003", "Brushless Impact Driver", "DEWALT", "impact driver", "cordless", "18 V", "1.1 kg", "N/A", "Yes", "$189.99", "0", "https://img.example.com/pt1003.jpg", "High torque fastening in a very short head length.", "3"],
    ["PT-1004", "Circular Saw 190mm", "Makita", "circular saw", "corded", "230 V", "4.1 kg", "-", "No", "$179.00", "9", "https://img.example.com/pt1004.jpg", "Sheet goods and framing saw with cast base and riving knife.", "2"],
    ["PT-1005", "Cordless Circular Saw", "makita", "circular saw", "cordless", "36 V", "3.6 kg", "-", "Yes", "$349.00", "3", "https://img.example.com/pt1005.jpg", "Twin battery saw sized for site work away from power.", "2"],
    ["PT-1006", "Random Orbital Sander", "Bosch", "sander", "corded", "230 V", "1.3 kg", "N/A", "No", "$89.99", "21", "https://img.example.com/pt1006.jpg", "Fine finishing sander with dust extraction port.", "2"],
    ["PT-1007", "Angle Grinder 125mm", "Bosch", "grinder", "corded", "230 V", "2.2 kg", "TBD", "No", "$74.00", "17", "https://img.example.com/pt1007.jpg", "Metal cutting and surface prep grinder with restart protection.", "2"],
    ["PT-1008", "Cordless Angle Grinder", "bosch", "grinder", "cordless", "18 V", "2.0 kg", "?", "Yes", "$229.00", "4", "https://img.example.com/pt1008.jpg", "Brushless grinder with kickback brake for site work.", "2"],
    ["PT-1009", "Reciprocating Saw", "Milwaukee", "reciprocating saw", "cordless", "18 V", "3.0 kg", "", "Yes", "$259.00", "5", "https://img.example.com/pt1009.jpg", "Demolition saw for timber, nail-embedded stock and pipe.", "5"],
    ["PT-1010", "Compact Jigsaw", "Milwaukee", "jigsaw", "cordless", "12 V", "1.6 kg", "N/A", "Yes", "$169.00", "8", "https://img.example.com/pt1010.jpg", "Curve cutting in sheet material with tool-free blade change.", "5"],
    ["PT-1011", "Rotary Hammer SDS Plus", "Hilti", "rotary hammer", "corded", "230 V", "3.9 kg", "-", "No", "$1,299.00", "2", "https://img.example.com/pt1011.jpg", "Anchor drilling and light chiselling in reinforced concrete.", "2"],
    ["PT-1012", "Cordless Rotary Hammer", "Hilti", "rotary hammer", "cordless", "36 V", "4.3 kg", "-", "Yes", "$1,449.00", "0", "https://img.example.com/pt1012.jpg", "Cable free anchor setting with active vibration reduction.", "2"],
    ["PT-1013", "Palm Router", "Ryobi", "router", "corded", "230 V", "1.5 kg", "8 mm", "No", "$99.00", "11", "https://img.example.com/pt1013.jpg", "Edge trimming and small dados in one hand.", "3"],
    ["PT-1014", "Plunge Router 1/2in", "Ryobi", "router", "corded", "230 V", "5.2 kg", "12 mm", "No", "$219.00", "3", "https://img.example.com/pt1014.jpg", "Template work and deeper mortises with fine height adjust.", "3"],
    ["PT-1015", "Cordless Drill Driver 12V", "Ryobi", "drill", "cordless", "12 V", "0.9 kg", "10 mm", "Yes", "$79.00", "32", "https://img.example.com/pt1015.jpg", "Household repairs and flat pack assembly, very light in hand.", "3"],
    ["PT-1016", "Combi Drill 18V", "Ryobi", "drill", "cordless", "18 V", "1.7 kg", "13 mm", "No", "$119.00", "18", "https://img.example.com/pt1016.jpg", "Masonry capable combi for occasional heavier jobs.", "3"],
    ["PT-1017", "Table Saw 254mm", "Metabo", "table saw", "corded", "230 V", "24.5 kg", "N/A", "No", "$629.00", "1", "https://img.example.com/pt1017.jpg", "Rip capacity for sheet breakdown in a small shop.", "3"],
    ["PT-1018", "Mitre Saw 216mm", "Metabo", "mitre saw", "corded", "230 V", "13.8 kg", "-", "No", "$399.00", "4", "https://img.example.com/pt1018.jpg", "Repeatable crosscuts for trim and framing.", "3"],
    ["PT-1019", "Cordless Multi Tool", "Einhell", "multi tool", "cordless", "18 V", "1.2 kg", "N/A", "No", "$89.00", "13", "https://img.example.com/pt1019.jpg", "Flush cuts, scraping and sanding in awkward spots.", "2"],
    ["PT-1020", "Corded Heat Gun", "Einhell", "heat gun", "corded", "230 V", "0.8 kg", "N/A", "No", "$39.99", "26", "https://img.example.com/pt1020.jpg", "Paint stripping and shrink fitting with two air settings.", "2"],
    ["PT-1021", "Cordless Nailer 18ga", "Milwaukee", "nailer", "cordless", "18 V", "3.2 kg", "-", "Yes", "$479.00", "0", "https://img.example.com/pt1021.jpg", "Second fix trim nailing with no hose and no compressor.", "5"],
    ["PT-1022", "Belt Sander 76mm", "Makita", "sander", "corded", "230 V", "4.6 kg", "N/A", "No", "$199.00", "7", "https://img.example.com/pt1022.jpg", "Fast stock removal on wide flat timber surfaces.", "2"],
    ["PT-1023", "Cordless Impact Wrench", "Makita", "impact wrench", "cordless", "18 V", "2.5 kg", "-", "Yes", "$339.00", "6", "https://img.example.com/pt1023.jpg", "Automotive and structural fastening at high breakaway torque.", "2"],
    ["PT-1024", "Bench Grinder 150mm", "Einhell", "grinder", "corded", "230 V", "9.4 kg", "N/A", "No", "call for quote", "5", "https://img.example.com/pt1024.jpg", "Tool sharpening and deburring on a fixed bench mount.", "TBD"],
    ["PT-1025", "Cordless Blower", "Ryobi", "blower", "cordless", "18 V", "1.9 kg", "N/A", "No", "$69.00", "0", "https://img.example.com/pt1025.jpg", "Site and yard clearing on the same battery platform.", "3"],
    ["PT-1026", "SDS Max Demolition Hammer", "Hilti", "demolition hammer", "corded", "230 V", "10.2 kg", "-", "No", "$2,150.00", "1", "https://img.example.com/pt1026.jpg", "Breaking slab and heavy chasing work all day.", "2"],
]

# --- Catalog B -------------------------------------------------------------------------

TEA_HEADER = [
    "Item Code",
    "Product Name",
    "Origin Country",
    "Leaf Grade",
    "Flavour Notes",
    "Certifications",
    "Caffeine Level",
    "Net Weight",
    "Retail Price",
    "Units In Stock",
    "Image Link",
    "Tasting Description",
    "",
]

TEAS = [
    ["TEA-201", "Assam Second Flush", "India", "TGFOP", "malty | honey | stonefruit", "organic; fair-trade", "High", "100 g", "12,50 €", "40", "https://img.example.com/tea201.jpg", "A brisk breakfast cup that stands up to milk without turning thin.", ""],
    ["TEA-202", "Darjeeling First Flush", "India", "FTGFOP1", "muscatel | floral | citrus", "organic", "Medium", "50 g", "24,00 €", "12", "https://img.example.com/tea202.jpg", "Delicate spring pluck, best taken clear and slightly under-steeped.", ""],
    ["TEA-203", "Sencha Fukamushi", "Japan", "Deep Steamed", "grassy | umami | seaweed", "organic; single-estate", "Medium", "80 g", "18,90 €", "22", "https://img.example.com/tea203.jpg", "Cloudy green liquor with a thick savoury body and a short finish.", ""],
    ["TEA-204", "Gyokuro Reserve", "Japan", "Shaded", "umami | sweet | marine", "single-estate", "High", "30 g", "129,00 €", "3", "https://img.example.com/tea204.jpg", "Shade grown and intensely savoury, brewed cool and slow.", ""],
    ["TEA-205", "Silver Needle White", "China", "Bai Hao Yinzhen", "melon | hay | honeysuckle", "organic", "Low", "50 g", "46,00 €", "7", "https://img.example.com/tea205.jpg", "Downy buds only, gentle and forgiving of a long steep.", ""],
    ["TEA-206", "Jasmine Pearls", "China", "Hand Rolled", "jasmine | cream | orchid", "fair-trade", "Medium", "100 g", "21,50 €", "18", "https://img.example.com/tea206.jpg", "Scented over fresh blossom for several nights, floral without soap.", ""],
    ["TEA-207", "Tie Guan Yin Oolong", "China", "Anxi", "orchid | butter | lilac", "N/A", "Medium", "100 g", "27,00 €", "9", "https://img.example.com/tea207.jpg", "Rolled green oolong that opens over many short infusions.", ""],
    ["TEA-208", "Da Hong Pao Rock Oolong", "China", "Wuyi", "mineral | roast | dark fruit", "single-estate", "Medium", "50 g", "58,00 €", "0", "https://img.example.com/tea208.jpg", "Charcoal roasted cliff tea, mineral and long in the throat.", ""],
    ["TEA-209", "Shou Puerh 2016", "China", "Ripe", "earth | wood | date", "-", "Medium", "250 g", "34,00 €", "11", "https://img.example.com/tea209.jpg", "Fully fermented and mellow, an easy introduction to aged tea.", ""],
    ["TEA-210", "Sheng Puerh 2009", "China", "Raw", "camphor | apricot | smoke", "single-estate", "High", "357 g", "1.299,00 €", "1", "https://img.example.com/tea210.jpg", "A collector cake, still bracing and improving in storage.", ""],
    ["TEA-211", "Ceylon Orange Pekoe", "Sri Lanka", "OP", "citrus | brisk | woody", "fair-trade", "High", "200 g", "9,80 €", "55", "https://img.example.com/tea211.jpg", "The everyday pot, clean and bright with or without milk.", ""],
    ["TEA-212", "Nilgiri Frost Tea", "India", "SFTGFOP", "eucalyptus | plum | mint", "organic; fair-trade", "Medium", "100 g", "16,40 €", "0", "https://img.example.com/tea212.jpg", "Cold season pluck with a cooling aromatic lift.", ""],
    ["TEA-213", "Rooibos Vanilla", "South Africa", "Long Cut", "vanilla | woody | sweet", "organic; caffeine-free", "None", "150 g", "8,90 €", "48", "https://img.example.com/tea213.jpg", "Naturally sweet and free of caffeine, fine late in the evening.", "clearance"],
    ["TEA-214", "Honeybush Original", "South Africa", "Long Cut", "honey | apricot | soft", "organic; caffeine-free", "None", "150 g", "9,40 €", "31", "https://img.example.com/tea214.jpg", "Rounder and sweeter than rooibos, very hard to over-steep.", ""],
    ["TEA-215", "Chamomile Whole Flower", "Egypt", "Whole Flower", "apple | hay | honey", "organic; caffeine-free", "None", "75 g", "11,20 €", "26", "https://img.example.com/tea215.jpg", "Whole heads rather than dust, clean and softly sweet.", ""],
    ["TEA-216", "Peppermint Leaf", "Egypt", "Cut Leaf", "mint | cooling | sharp", "organic; caffeine-free", "None", "100 g", "7,50 €", "37", "https://img.example.com/tea216.jpg", "Bracing after a heavy meal, no bitterness on a long steep.", ""],
    ["TEA-217", "Lapsang Souchong", "China", "Smoked", "pine smoke | resin | dried plum", "-", "Medium", "100 g", "14,60 €", "14", "https://img.example.com/tea217.jpg", "Pinewood smoked over several days, divisive and unmistakable.", ""],
    ["TEA-218", "Earl Grey Supreme", "Blend", "Blended", "bergamot | citrus | cornflower", "fair-trade", "High", "125 g", "13,90 €", "29", "https://img.example.com/tea218.jpg", "Black base with real bergamot oil rather than flavouring.", ""],
    ["TEA-219", "Masala Chai Blend", "India", "CTC", "cardamom | ginger | clove", "fair-trade; organic", "High", "250 g", "15,00 €", "20", "https://img.example.com/tea219.jpg", "Built for boiling with milk, spice forward and robust.", ""],
    ["TEA-220", "Genmaicha", "Japan", "Bancha", "toasted rice | nutty | mild", "organic", "Low", "150 g", "10,80 €", "24", "https://img.example.com/tea220.jpg", "Toasted rice softens the green tea, comforting and low caffeine.", ""],
    ["TEA-221", "Matcha Ceremonial", "Japan", "Stone Ground", "umami | sweet | grassy", "organic; single-estate", "High", "30 g", "39,00 €", "6", "https://img.example.com/tea221.jpg", "First harvest leaf, whisked rather than steeped.", ""],
    ["TEA-222", "Hojicha Roasted", "Japan", "Roasted Bancha", "roast | caramel | woody", "organic", "Low", "100 g", "12,00 €", "0", "https://img.example.com/tea222.jpg", "Roasted until nutty and brown, gentle enough for late afternoon.", ""],
    ["TEA-223", "Golden Monkey Black", "China", "Superior", "cocoa | malt | sweet potato", "TBD", "Medium", "100 g", "26,50 €", "8", "https://img.example.com/tea223.jpg", "Golden tipped and naturally sweet, no milk needed.", ""],
    ["TEA-224", "Milk Oolong", "Taiwan", "Jin Xuan", "cream | coconut | floral", "single-estate", "Medium", "75 g", "23,00 €", "15", "https://img.example.com/tea224.jpg", "Naturally creamy cultivar grown at moderate elevation.", ""],
    ["TEA-225", "Ali Shan High Mountain", "Taiwan", "High Mountain", "floral | buttery | sugarcane", "organic; single-estate", "Medium", "75 g", "preis auf anfrage", "4", "https://img.example.com/tea225.jpg", "Grown above 1000 metres, thick textured and very aromatic.", ""],
    ["TEA-226", "Hibiscus Petals", "Nigeria", "Cut Petal", "tart | cranberry | floral", "organic; caffeine-free", "None", "125 g", "8,20 €", "33", "https://img.example.com/tea226.jpg", "Sharply tart and deep red, good iced with a little sugar.", ""],
]

# Two junk rows above the header, as real exports carry.
TEA_PREAMBLE = [
    ["Harbourside Tea Merchants — Wholesale Catalogue", "", "", "", "", "", "", "", "", "", "", "", ""],
    ["Prices valid until 31/12 — contact sales@example.com", "", "", "", "", "", "", "", "", "", "", "", ""],
]

TEA_README = [
    ["Internal notes"],
    ["Do not send this sheet to customers."],
    ["Stock figures refresh nightly."],
]


def write_power_tools(path: Path) -> None:
    # utf-8-sig: Excel writes a BOM, and a loader that cannot cope produces a mangled
    # first column name.
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(POWER_TOOL_HEADER)
        writer.writerows(POWER_TOOLS)


def write_teas(path: Path) -> None:
    rows = TEA_PREAMBLE + [TEA_HEADER] + TEAS
    frame = pd.DataFrame(rows)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name="Catalogue", index=False, header=False)
        pd.DataFrame(TEA_README).to_excel(
            writer, sheet_name="Read Me", index=False, header=False
        )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    write_power_tools(OUT / "power_tools.csv")
    write_teas(OUT / "tea_and_infusions.xlsx")
    print(f"wrote {len(POWER_TOOLS)} rows -> {OUT / 'power_tools.csv'}")
    print(f"wrote {len(TEAS)} rows -> {OUT / 'tea_and_infusions.xlsx'}")


if __name__ == "__main__":
    main()
