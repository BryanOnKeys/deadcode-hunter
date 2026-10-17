"""__main__ block handling."""


def main():
    print("hi")


def _private_main():
    print("private")


if __name__ == "__main__":

    def _runner():
        main()

    _runner()
