from storage import get_booking, initialize_database


def main():
    initialize_database()
    booking_id = input("Enter a booking ID: ").strip()

    if not booking_id:
        print("Please enter a booking ID. The ID cannot be empty.")
        return

    booking = get_booking(booking_id)

    if booking is None:
        print("No booking found with that ID.")
        return

    print("\nSaved booking")
    print(f"Booking ID: {booking['booking_id']}")
    print(f"Service: {booking['service_code']}")
    print(f"Quantity: {booking['quantity']} garments")
    print(f"Total price: Rs {booking['total_price']}")
    print(f"Pickup slot: {booking['slot_code']}")
    print(f"Status: {booking['status']}")


if __name__ == "__main__":
    main()
