"""python -m cloudstore.erasure.native  -> build the native kernel library."""
from .build import build

if __name__ == "__main__":
    print(f"built {build()}")
