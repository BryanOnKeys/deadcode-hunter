def compute_total(items):
    total = 0
    count = 0
    for item in items:
        total = total + item.price
        count = count + 1
    tax = total * 0.2
    average = total / count
    final = total + tax
    rounded = round(final, 2)
    return rounded, average


def unrelated():
    return 1
