import random
import secrets

_ADJECTIVES = [
    # A
    "able", "abiding", "agile", "airy", "alabaster", "alert", "alpine", "amber",
    "ample", "ancient", "ardent", "arctic", "arid", "ashen", "astral", "astute",
    "atomic", "auburn", "august", "avid", "azure",
    # B
    "balmy", "bare", "benevolent", "blazed", "blazing", "blithe", "blissful",
    "blooming", "blushing", "bold", "boreal", "boundless", "brave", "breezy",
    "bright", "brisk", "broad", "bronze", "buoyant", "burnished",
    # C
    "calm", "candid", "carefree", "caramel", "cedar", "celestial", "cerulean",
    "chatty", "cheerful", "chestnut", "chill", "civic", "clear", "clever",
    "clover", "cloudy", "coastal", "cobalt", "comet", "cool", "copper", "coral",
    "cordial", "cosmic", "courageous", "cozy", "creative", "crisp", "crystal",
    "curly", "cyan",
    # D
    "dainty", "dapper", "daring", "dark", "dashing", "dauntless", "dawn",
    "decisive", "deep", "devoted", "dewy", "diligent", "dim", "drifting",
    "durable", "dusty", "dynamic",
    # E
    "eager", "early", "earnest", "earthy", "easy", "ebony", "effervescent",
    "electric", "elegant", "eloquent", "emerald", "empty", "enduring", "endless",
    "energetic", "enlightened", "epic", "eternal", "everlasting", "even", "exact",
    "exquisite",
    # F
    "fabled", "fair", "faithful", "famous", "fancy", "far", "farsighted", "fast",
    "felicitous", "fern", "fervent", "festive", "fierce", "fiery", "fine", "firm",
    "first", "flexible", "flint", "floral", "flourishing", "flowing", "foggy",
    "fond", "forest", "forward", "fragrant", "frank", "free", "fresh", "frosty",
    "frozen", "full",
    # G
    "gallant", "gentle", "genuine", "gifted", "gilded", "glacial", "glad",
    "gleaming", "glorious", "glowing", "golden", "good", "graceful", "gracious",
    "grand", "granite", "grateful", "great", "green", "grey", "grounded",
    # H
    "halcyon", "hardy", "harmonious", "hazy", "hearty", "heroic", "high",
    "hollow", "honest", "hopeful", "humble",
    # I
    "icy", "idle", "idyllic", "illumined", "imaginative", "immense", "impeccable",
    "indigo", "infinite", "ingenious", "inland", "inner", "inspired", "intrepid",
    "inventive", "invincible", "iron", "ivory",
    # J
    "jade", "jolly", "jovial", "joyful", "joyous", "jubilant", "judicious", "just",
    # K
    "keen", "kind", "kindhearted", "kinetic",
    # L
    "lambent", "large", "lasting", "lavender", "leafy", "lean", "legendary",
    "lemon", "level", "light", "lively", "lone", "long", "loyal", "lucid",
    "lucky", "luminous", "lunar", "lush", "lyric",
    # M
    "magic", "magnanimous", "majestic", "maple", "marble", "mature", "measured",
    "meditative", "mellifluous", "mellow", "merciful", "methodical", "mighty",
    "mild", "mindful", "misty", "modest", "moonlit", "mossy", "mountainous", "muted",
    # N
    "narrow", "natural", "neat", "nimble", "noble", "nocturnal", "nordic",
    "north", "nurturing",
    # O
    "oak", "observant", "obsidian", "oceanic", "olive", "open", "opulent",
    "optimistic", "orange", "orderly", "outer",
    # P
    "pale", "passionate", "patient", "peaceful", "pearly", "perennial",
    "persevering", "petal", "pine", "pioneering", "plain", "playful", "poised",
    "polar", "polished", "pragmatic", "principled", "promising", "proud", "pure",
    "purple", "purposeful",
    # Q
    "quiet", "quickened", "quintessential",
    # R
    "radiant", "rapid", "rare", "ready", "receptive", "reflective", "refreshing",
    "regal", "reliable", "resilient", "resolute", "resourceful", "respectful",
    "responsive", "rich", "righteous", "roaming", "robust", "rooted", "rosy",
    "rough", "round", "royal", "ruby", "rugged", "running", "rustic", "rustling",
    # S
    "sacred", "sage", "sandy", "sapphire", "scholarly", "seasoned", "serene",
    "seraphic", "sharp", "shining", "silent", "silken", "silver", "simple",
    "sincere", "skillful", "sleek", "slender", "slow", "smart", "smooth", "snowy",
    "sociable", "solar", "solemn", "solid", "sonic", "soulful", "sovereign",
    "spirited", "steadfast", "sterling", "still", "stoic", "stormy", "stout",
    "strategic", "strong", "sturdy", "sublime", "subtle", "sunny", "supernal",
    "supple", "supportive", "swift",
    # T
    "talented", "tall", "tawny", "tender", "tenacious", "thoughtful", "thriving",
    "tidal", "tidy", "timeless", "timely", "tireless", "tranquil", "transparent",
    "triumphant", "true", "trusty", "trusting", "twilight",
    # U
    "ultra", "unassuming", "undaunted", "unwavering", "upbeat", "uplifting",
    "unyielding", "upright",
    # V
    "valiant", "vast", "velvet", "venerable", "venturous", "verdant", "vernal",
    "versatile", "vibrant", "vigilant", "visionary", "vital", "vivacious", "vivid",
    # W
    "warm", "watchful", "wavy", "whimsical", "wholesome", "willing", "windswept",
    "wise", "witty", "wondrous", "worthy", "woven",
    # X Y Z
    "xenial", "xeric", "yielding", "young", "youthful", "zealous", "zenith",
    "zephyr", "zestful", "zippy",
    # extras to reach 500
    "affable", "aloft", "animated", "artful", "balanced", "bountiful", "bracing",
    "candent", "capacious", "certain", "clean", "collected", "composed", "confident",
    "conscientious", "considerate", "constant", "deft", "deliberate", "dependable",
    "dignified", "discerning", "effulgent", "elevated", "eminent", "endeared",
    "equable", "established", "expansive", "expressive", "felicific", "forthright",
    "fulsome", "generative", "graced", "hallowed", "idiomatic", "imaginable",
    "immovable", "incisive", "inquisitive", "insightful", "integral", "intuitive",
    "jaunty", "knowing", "limber", "limpid", "lofty", "manifest",
    "masterful", "meticulous", "musing", "nascent", "nuanced",
    "openhearted", "ordinate", "pacific", "pellucid", "perceptive", "pious",
    "plentiful", "precise", "pristine", "progressing",
    "reasoned", "refined", "reposed", "revered", "rhapsodic", "sapient",
    "sanguine", "settled", "sheer", "sideral", "spiraling",
    "stately", "stellar", "temperate", "thorough", "tonal",
    "torchlit", "tractable", "transcendent", "unbounded", "unfailing", "unfettered",
]

_NOUNS = [
    # A
    "adder", "alder", "algae", "aloe", "anchor", "antler", "anvil", "apex",
    "apple", "arch", "arrow", "ash", "aspen", "atlas", "atoll", "aurora",
    "autumn", "avalanche", "axe",
    # B
    "badger", "bark", "barn", "basalt", "basin", "bay", "beacon", "bear",
    "beaver", "beech", "bell", "berry", "birch", "bison", "blade", "bloom",
    "blossom", "bluebell", "bluff", "boar", "bog", "boulder", "bough", "bramble",
    "brant", "bream", "brine", "brook", "buck", "bud", "burrow", "buttercup",
    "butte",
    # C
    "canoe", "canopy", "canyon", "cape", "cardinal", "carp", "catfish", "cave",
    "cedar", "channel", "charcoal", "chestnut", "chisel", "chough", "cicada",
    "cinder", "cirque", "cirrus", "clam", "cliff", "clover", "cobble", "cobalt",
    "cockle", "col", "comet", "compass", "condor", "coral", "cormorant", "corrie",
    "cove", "coyote", "crane", "crater", "creek", "crest", "crocus", "crow",
    "crystal", "curlew", "current", "cypress",
    # D
    "dale", "darter", "dawn", "deer", "delta", "dew", "dipper", "dock",
    "dolphin", "dormouse", "dotterel", "dove", "dragonfly", "drumlin", "dunlin",
    "dune", "dyke",
    # E
    "eagle", "echo", "eel", "egret", "elder", "elk", "elm", "ember",
    "escarpment", "estuary",
    # F
    "falcon", "fawn", "fern", "field", "fiord", "firecrest", "finch", "fir",
    "fjord", "flame", "flint", "flood", "flora", "fluke", "flycatcher", "foam",
    "fog", "forest", "forge", "fossil", "fox", "frost", "fulmar",
    # G
    "gale", "gannet", "garganey", "garnet", "geyser", "glacier", "glade",
    "glen", "godwit", "goldcrest", "goldfinch", "goosander", "gorge", "goshawk",
    "granite", "gravel", "grove", "grouse", "guillemot", "gulch", "gull", "gust",
    # H
    "hamster", "hare", "harrier", "hartebeest", "hawk", "hazel", "heath",
    "hedgehog", "heron", "hill", "hillside", "holly", "hoopoe", "horizon",
    "hornet",
    # I
    "ibex", "ibis", "inlet", "iris", "island", "ivory",
    # J
    "jackal", "jackdaw", "jasper", "jay", "juniper",
    # K
    "kelp", "kestrel", "kingfisher", "kite", "kiwi", "knoll", "knot",
    # L
    "lagoon", "lake", "lapwing", "larch", "lark", "lava", "leaf", "ledge",
    "lemur", "leveret", "lichen", "limestone", "linden", "linnet", "lion",
    "loam", "loess", "locust", "lodge", "loon", "lotus", "lynx",
    # M
    "mackerel", "magpie", "mallard", "maple", "marl", "marsh", "martin",
    "meadow", "merganser", "merlin", "mesa", "midstream", "milfoil", "mink",
    "mist", "moorland", "moose", "moss", "moth", "mountain", "mudflat",
    "mudstone", "muskrat",
    # N
    "narwhal", "needle", "newt", "nightingale", "nightjar", "noctule", "north",
    "nutmeg", "nuthatch",
    # O
    "oak", "opal", "orca", "osprey", "otter", "outcrop", "owl", "oyster",
    "oystercatcher",
    # P
    "partridge", "pass", "pasture", "peak", "peat", "pebble", "peewit",
    "pelican", "pennant", "perch", "peregrine", "petal", "pheasant", "pigeon",
    "pine", "pintail", "pipit", "plover", "plume", "pochard", "polecat",
    "pond", "poplar", "porcupine", "prairie", "ptarmigan", "puffin",
    # Q
    "quartz", "quill",
    # R
    "rabbit", "raven", "reed", "reedbed", "reef", "ridge", "rift", "rill",
    "ringlet", "ripple", "robin", "rock", "rockpool", "roe", "roost",
    "rosefinch", "root", "rowan", "runnel", "rush", "rushes",
    # S
    "sage", "salmon", "sandbar", "sandpiper", "sandstone", "sapphire", "scree",
    "seabird", "seagrass", "sedge", "serin", "sett", "shale", "shearwater",
    "shoal", "shore", "shoveler", "shrew", "shrike", "sierra", "siskin",
    "skimmer", "skua", "skylark", "slate", "sluice", "smew", "snipe",
    "snowdrop", "snowfield", "snout", "sora", "sorrel", "sparrow", "spire",
    "spoonbill", "spruce", "squall", "squirrel", "stag", "starling", "stem",
    "steppe", "stoat", "stone", "stonefly", "stork", "storm", "stream",
    "stubble", "summit", "sundew", "sunfish", "swallow", "swamp", "swan",
    "swift", "sycamore",
    # T
    "talon", "tanager", "tarpon", "teal", "tern", "thistle", "thorn",
    "thornbill", "thicket", "thrush", "tide", "timber", "titlark", "toad",
    "torchwood", "torrent", "trail", "treecreeper", "trogon", "trout", "tuatara",
    "tumulus", "tundra", "turnstone", "turtle", "twite",
    # U
    "upland",
    # V
    "vale", "valley", "veery", "verdure", "vetch", "viburnum", "vireo",
    "viper", "vixen", "vole",
    # W
    "wader", "wagtail", "warbler", "waterfowl", "watershed", "wave", "waxwing",
    "weasel", "weir", "wheatear", "whinchat", "whitethroat", "wigeon", "wildcat",
    "willow", "wolf", "woodcock", "woodland", "woodpecker", "wormwood", "wren",
    "wryneck",
    # Y Z
    "yarrow", "yellowhammer", "yew", "zander", "zinnia", "zorilla",
    # extras to reach 500
    "acorn", "agate", "albatross", "alcove", "almond", "alp", "amaranth",
    "amethyst", "anemone", "antelope", "arroyo", "azalea", "barnacle", "bayou",
    "blizzard", "bracken", "brushwood", "buffalo",
    "bulrush", "bunting", "bushtit", "buzzard", "capercaillie", "caribou",
    "catkin", "chaffinch", "chalcedony", "chamomile", "cheetah", "cinnabar",
    "cistus", "clough", "coho", "columbine", "coppice", "crag", "crake",
    "crossbill", "crowberry", "dace", "daisy", "damselfly", "dandelion",
    "dewdrop", "dhole", "dingo", "dunock", "earthworm", "echidna", "ermine",
    "esker", "fen", "flounder", "flyway", "forb", "gallinule", "garfish",
    "gecko", "geode", "gerbil", "gibbon", "ginkgo", "gloaming", "glowworm",
    "gneiss", "gnu",
]


def generate_username():
    from mwmbl.models import MwmblUser
    for _ in range(10):
        candidate = f"{random.choice(_ADJECTIVES)}_{random.choice(_NOUNS)}_{random.randint(100, 999)}"
        if not MwmblUser.objects.filter(username=candidate).exists():
            return candidate
    # Extremely unlikely fallback — collision exhausted after 10 attempts
    return f"user_{secrets.token_hex(6)}"
