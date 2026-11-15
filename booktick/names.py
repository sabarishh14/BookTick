"""A friendly name for each browser, like "Brave Otter 27", so nobody's watchers are all called "Me".

It comes from the browser's owner id (a hash of its private key), so the same browser always
gets the same name, and so does any device opened with that browser's watcher link. People can
still type their own name in the form.
"""

ADJECTIVES = [
    "Brave", "Calm", "Clever", "Cosmic", "Daring", "Eager", "Fancy", "Gentle", "Golden", "Happy",
    "Jolly", "Keen", "Lively", "Lucky", "Mellow", "Mighty", "Nimble", "Noble", "Plucky", "Quick",
    "Quiet", "Rapid", "Royal", "Sunny", "Swift", "Witty", "Zesty", "Bold", "Bright", "Cheery",
    "Crisp", "Dapper", "Fearless", "Fizzy", "Breezy", "Groovy", "Hearty", "Jazzy", "Merry", "Peppy",
]
ANIMALS = [
    "Otter", "Tiger", "Falcon", "Panda", "Koala", "Dolphin", "Fox", "Owl", "Lynx", "Heron",
    "Badger", "Bison", "Cheetah", "Crane", "Eagle", "Gecko", "Hawk", "Ibis", "Jaguar", "Kiwi",
    "Lemur", "Macaw", "Narwhal", "Ocelot", "Peacock", "Puffin", "Quokka", "Raven", "Seal", "Sparrow",
    "Tapir", "Toucan", "Walrus", "Yak", "Zebra", "Mongoose", "Elephant", "Leopard", "Pelican", "Squirrel",
]


def name_for(owner):
    """'Brave Otter 27' for an owner id (hex). About 140,000 different names."""
    try:
        n = int(str(owner)[:12], 16)
    except ValueError:
        n = sum(ord(c) for c in str(owner))
    adjective = ADJECTIVES[n % len(ADJECTIVES)]
    animal = ANIMALS[(n // len(ADJECTIVES)) % len(ANIMALS)]
    number = (n // (len(ADJECTIVES) * len(ANIMALS))) % 90 + 10
    return f"{adjective} {animal} {number}"
