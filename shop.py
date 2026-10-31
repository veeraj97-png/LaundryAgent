# Fictional prices for our learning prototype.
# All prices are in Indian rupees, per garment.
from copy import deepcopy
from uuid import uuid4
from scheduling import validate_pickup_date
from storage import (
    initialize_database, read_pickup_slots, save_booking, save_proposal,
    record_proposal_decision as record_stored_decision,
    supersede_proposal as supersede_stored_proposal,
)

services = {
    "wash_and_fold": {
        "display_name": "Wash and Fold",
        "price_per_item": 30,
        "turnaround_days": 2,
    },
    "wash_and_iron": {
        "display_name": "Wash and Iron",
        "price_per_item": 50,
        "turnaround_days": 3,
    },
}



def calculate_quote(service_code, quantity):
    if service_code not in services:
        raise ValueError("Please choose a supported laundry service.")

    if type(quantity) is not int or quantity <= 0:
        raise ValueError("Quantity must be a positive whole number.")

    service = services[service_code]
    unit_price = service["price_per_item"]
    total_price = unit_price * quantity

    return {
        "service_code": service_code,
        "service_name": service["display_name"],
        "quantity": quantity,
        "unit_price": unit_price,
        "total_price": total_price,
        "turnaround_days": service["turnaround_days"],
    }
def check_pickup_slot(slot_code, pickup_date=None):
    pickup_date = validate_pickup_date(pickup_date)
    for slot in read_pickup_slots(pickup_date):
        if slot["slot_code"] == slot_code:
            return {
                **slot,
                "available": slot["remaining_capacity"] > 0,
            }

    raise ValueError("Please choose a supported pickup slot.")

def get_available_pickup_slots(pickup_date=None):
    available_slots = []

    pickup_date = validate_pickup_date(pickup_date)
    for slot in read_pickup_slots(pickup_date):
        if slot["remaining_capacity"] > 0:
            available_slots.append({
                **slot,
                "available": True,
            })

    return available_slots


def build_booking_proposal(service_code, quantity, slot_code, pickup_date=None, *, request_id=None):
    """Build terms without writing, so a graph can checkpoint them first."""
    quote = calculate_quote(service_code, quantity)
    pickup = check_pickup_slot(slot_code, pickup_date)

    if not pickup["available"]:
        raise ValueError("That pickup slot is full.")

    return {
        "request_id": str(uuid4()) if request_id is None else request_id,
        "quote": quote,
        "pickup": pickup,
        "status": "awaiting_approval",
    }



def prepare_booking_proposal(service_code, quantity, slot_code, pickup_date=None):
    proposal = build_booking_proposal(service_code, quantity, slot_code, pickup_date)
    stored = save_proposal(proposal)
    proposal["snapshot_id"] = stored["snapshot_id"]
    return proposal


def record_proposal_decision(request_id, snapshot_id, decision):
    """Trusted human application action; never exposed as a model tool."""
    return record_stored_decision(request_id, snapshot_id, decision)


def supersede_proposal(request_id, snapshot_id):
    return supersede_stored_proposal(request_id, snapshot_id)


def create_booking(proposal):

    booking = {
        "request_id": proposal["request_id"],
        "snapshot_id": proposal.get("snapshot_id"),
        "booking_id": str(uuid4()),
        "quote": deepcopy(proposal["quote"]),
        "pickup": deepcopy(proposal["pickup"]),
        "status": "confirmed",
    }

    booking = save_booking(booking)
    proposal["status"] = "booked"

    return booking


def main():
    for service_code, details in services.items():
        print(
            f"{details['display_name']}: "
            f"Rs {details['price_per_item']} per item, "
            f"{details['turnaround_days']} days"
        )

    initialize_database()

    selected_slot = None

    while True:
        entered_date = input("Pickup date (YYYY-MM-DD), or quit: ").strip()
        if entered_date.lower() == "quit":
            print("Pickup selection cancelled.")
            return
        try:
            pickup_date = validate_pickup_date(entered_date)
            break
        except ValueError as error:
            print(str(error))

    # Ask the customer to select a pickup slot.
    while True:
        requested_slot = input(
            "\nChoose a pickup slot (morning / afternoon / evening), "
            "or type quit: "
        ).strip().lower()

        if requested_slot == "quit":
            print("Pickup selection cancelled.")
            break

        try:
            pickup = check_pickup_slot(requested_slot, pickup_date)

        
            if pickup["available"]:
                selected_slot = requested_slot
                print(f"\nSelected pickup: {pickup['display_name']}")
                print("No booking has been created yet.")
                break

            print(f"\nRequested slot full: {pickup['display_name']}")
            alternatives = get_available_pickup_slots(pickup_date)

            if not alternatives:
                print("No pickup slots are currently available.")
                break

            print("Available alternatives:")

            for alternative in alternatives:
                print(
                    f"- {alternative['slot_code']}: "
                    f"{alternative['display_name']}"
                )

        except ValueError as error:
            print(f"\nCannot check pickup: {error}")


    # Separate example: prepare a proposal using fixed sample values.
    if selected_slot is not None:
        try:
            proposal = prepare_booking_proposal(
                "wash_and_iron",
                5,
                selected_slot,
                pickup_date,
            )

            print("\nBooking proposal")
            print(f"Proposal ID: {proposal['request_id']}")
            print(f"Snapshot: {proposal['snapshot_id']}")
            print(f"Service: {proposal['quote']['service_name']}")
            print(f"Service code: {proposal['quote']['service_code']}")
            print(f"Quantity: {proposal['quote']['quantity']} garments")
            print(f"Unit price: Rs {proposal['quote']['unit_price']}")
            print(f"Estimated total: Rs {proposal['quote']['total_price']}")
            print(f"Turnaround: {proposal['quote']['turnaround_days']} days")
            print(f"Pickup: {proposal['pickup']['display_name']}")
            print(f"Pickup code: {proposal['pickup']['slot_code']}")
            print(f"Pickup date: {proposal['pickup']['pickup_date']}")
            print(f"Status: {proposal['status']}")
            while True:
                approval = input(
                    "\nDo you approve this proposal? (yes / no): "
                ).strip().lower()

                if approval == "yes":
                    record_proposal_decision(proposal["request_id"], proposal["snapshot_id"], "approved")
                    booking = create_booking(proposal)

                    print("\nBooking confirmed!")
                    print(f"Booking ID: {booking['booking_id']}")
                    print(f"Status: {booking['status']}")
                    updated_slot = check_pickup_slot(selected_slot, pickup_date)
                    print(
                        "Remaining pickup capacity: "
                        f"{updated_slot['remaining_capacity']}"
                    )
                    break 

                elif approval == "no":
                    record_proposal_decision(proposal["request_id"], proposal["snapshot_id"], "rejected")
                    print("Proposal rejected. No booking will be created.")
                    break

                else:
                    print("Please enter yes or no.")

        except ValueError as error:
            print(f"\nCannot prepare proposal: {error}")


if __name__ == "__main__":
    main()
