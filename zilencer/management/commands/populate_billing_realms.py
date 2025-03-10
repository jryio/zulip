import contextlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import stripe
from django.core.management.base import BaseCommand, CommandParser
from django.utils.timezone import now as timezone_now
from typing_extensions import override

from corporate.lib.stripe import RealmBillingSession, RemoteServerBillingSession, add_months
from corporate.models import Customer, CustomerPlan, LicenseLedger
from scripts.lib.zulip_tools import TIMESTAMP_FORMAT
from zerver.actions.create_realm import do_create_realm
from zerver.actions.create_user import do_create_user
from zerver.actions.streams import bulk_add_subscriptions
from zerver.apps import flush_cache
from zerver.lib.streams import create_stream_if_needed
from zerver.models import Realm, UserProfile, get_realm
from zilencer.models import RemoteZulipServer
from zproject.config import get_secret

current_time = timezone_now().strftime(TIMESTAMP_FORMAT)


@dataclass
class CustomerProfile:
    unique_id: str
    billing_schedule: int = CustomerPlan.BILLING_SCHEDULE_ANNUAL
    tier: Optional[int] = None
    new_plan_tier: Optional[int] = None
    automanage_licenses: bool = False
    status: int = CustomerPlan.ACTIVE
    sponsorship_pending: bool = False
    is_sponsored: bool = False
    card: str = ""
    charge_automatically: bool = True
    renewal_date: str = current_time
    end_date: str = "2030-10-10-01-10-10"


class Command(BaseCommand):
    help = "Populate database with different types of realms that can exist."

    @override
    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--only-remote-server",
            action="store_true",
            help="Whether to only run for remote servers",
        )

    @override
    def handle(self, *args: Any, **options: Any) -> None:
        # Create a realm for each plan type

        customer_profiles = [
            # NOTE: The unique_id has to be less than 40 characters.
            CustomerProfile(unique_id="sponsorship-pending", sponsorship_pending=True),
            CustomerProfile(
                unique_id="annual-free",
                billing_schedule=CustomerPlan.BILLING_SCHEDULE_ANNUAL,
            ),
            CustomerProfile(
                unique_id="annual-standard",
                billing_schedule=CustomerPlan.BILLING_SCHEDULE_ANNUAL,
                tier=CustomerPlan.TIER_CLOUD_STANDARD,
            ),
            CustomerProfile(
                unique_id="annual-plus",
                billing_schedule=CustomerPlan.BILLING_SCHEDULE_ANNUAL,
                tier=CustomerPlan.TIER_CLOUD_PLUS,
            ),
            CustomerProfile(
                unique_id="monthly-free",
                billing_schedule=CustomerPlan.BILLING_SCHEDULE_MONTHLY,
            ),
            CustomerProfile(
                unique_id="monthly-standard",
                billing_schedule=CustomerPlan.BILLING_SCHEDULE_MONTHLY,
                tier=CustomerPlan.TIER_CLOUD_STANDARD,
            ),
            CustomerProfile(
                unique_id="monthly-plus",
                billing_schedule=CustomerPlan.BILLING_SCHEDULE_MONTHLY,
                tier=CustomerPlan.TIER_CLOUD_PLUS,
            ),
            CustomerProfile(
                unique_id="downgrade-end-of-cycle",
                billing_schedule=CustomerPlan.BILLING_SCHEDULE_MONTHLY,
                tier=CustomerPlan.TIER_CLOUD_STANDARD,
                status=CustomerPlan.DOWNGRADE_AT_END_OF_CYCLE,
            ),
            CustomerProfile(
                unique_id="standard-automanage-licenses",
                billing_schedule=CustomerPlan.BILLING_SCHEDULE_MONTHLY,
                tier=CustomerPlan.TIER_CLOUD_STANDARD,
                automanage_licenses=True,
            ),
            CustomerProfile(
                unique_id="standard-automatic-card",
                billing_schedule=CustomerPlan.BILLING_SCHEDULE_MONTHLY,
                tier=CustomerPlan.TIER_CLOUD_STANDARD,
                card="pm_card_visa",
            ),
            CustomerProfile(
                unique_id="standard-invoice-payment",
                billing_schedule=CustomerPlan.BILLING_SCHEDULE_MONTHLY,
                tier=CustomerPlan.TIER_CLOUD_STANDARD,
                charge_automatically=False,
            ),
            CustomerProfile(
                unique_id="standard-switch-to-annual-eoc",
                billing_schedule=CustomerPlan.BILLING_SCHEDULE_MONTHLY,
                tier=CustomerPlan.TIER_CLOUD_STANDARD,
                status=CustomerPlan.SWITCH_TO_ANNUAL_AT_END_OF_CYCLE,
            ),
            CustomerProfile(
                unique_id="sponsored",
                is_sponsored=True,
                billing_schedule=CustomerPlan.BILLING_SCHEDULE_MONTHLY,
                tier=CustomerPlan.TIER_CLOUD_STANDARD,
            ),
            CustomerProfile(
                unique_id="free-trial",
                tier=CustomerPlan.TIER_CLOUD_STANDARD,
                status=CustomerPlan.FREE_TRIAL,
            ),
            # Use `server` keyword in the unique_id to indicate that this is a profile for remote server.
            CustomerProfile(
                unique_id="legacy-server",
                tier=CustomerPlan.TIER_SELF_HOSTED_LEGACY,
            ),
            CustomerProfile(
                unique_id="legacy-server-upgrade-scheduled",
                tier=CustomerPlan.TIER_SELF_HOSTED_LEGACY,
                status=CustomerPlan.SWITCH_PLAN_TIER_AT_PLAN_END,
                new_plan_tier=CustomerPlan.TIER_SELF_HOSTED_PLUS,
            ),
            CustomerProfile(
                unique_id="business-server",
                tier=CustomerPlan.TIER_SELF_HOSTED_BUSINESS,
            ),
            CustomerProfile(
                unique_id="business-server-payment-starts-in-future",
                tier=CustomerPlan.TIER_SELF_HOSTED_BUSINESS,
            ),
        ]

        # Delete all existing remote servers
        RemoteZulipServer.objects.all().delete()
        flush_cache(None)

        servers = []
        for customer_profile in customer_profiles:
            if "server" in customer_profile.unique_id:
                server_conf = populate_remote_server(customer_profile)
                servers.append(server_conf)
            elif not options.get("only_remote_server"):
                populate_realm(customer_profile)

        print("-" * 40)
        for server in servers:
            for key, value in server.items():
                print(f"{key}: {value}")
            print("-" * 40)


def populate_realm(customer_profile: CustomerProfile) -> None:
    unique_id = customer_profile.unique_id
    if customer_profile.tier is None:
        plan_type = Realm.PLAN_TYPE_LIMITED
    elif (
        customer_profile.tier == CustomerPlan.TIER_CLOUD_STANDARD and customer_profile.is_sponsored
    ):
        plan_type = Realm.PLAN_TYPE_STANDARD_FREE
    elif customer_profile.tier == CustomerPlan.TIER_CLOUD_STANDARD:
        plan_type = Realm.PLAN_TYPE_STANDARD
    elif customer_profile.tier == CustomerPlan.TIER_CLOUD_PLUS:
        plan_type = Realm.PLAN_TYPE_PLUS
    else:
        raise AssertionError("Unexpected tier!")
    plan_name = Realm.ALL_PLAN_TYPES[plan_type]

    # Delete existing realm with this name
    with contextlib.suppress(Realm.DoesNotExist):
        get_realm(unique_id).delete()
        # Because we just deleted a bunch of objects in the database
        # directly (rather than deleting individual objects in Django,
        # in which case our post_save hooks would have flushed the
        # individual objects from memcached for us), we need to flush
        # memcached in order to ensure deleted objects aren't still
        # present in the memcached cache.
        flush_cache(None)

    realm = do_create_realm(
        string_id=unique_id,
        name=unique_id,
        description=unique_id,
        plan_type=plan_type,
    )

    # Create a user with billing access
    full_name = f"{plan_name}-admin"
    email = f"{full_name}@zulip.com"
    user = do_create_user(
        email,
        full_name,
        realm,
        full_name,
        role=UserProfile.ROLE_REALM_OWNER,
        acting_user=None,
    )

    stream, _ = create_stream_if_needed(
        realm,
        "all",
    )

    bulk_add_subscriptions(realm, [stream], [user], acting_user=None)

    if customer_profile.sponsorship_pending:
        customer = Customer.objects.create(
            realm=realm,
            sponsorship_pending=customer_profile.sponsorship_pending,
        )
        return

    if customer_profile.tier is None:
        return

    billing_session = RealmBillingSession(user)
    customer = billing_session.update_or_create_stripe_customer()
    assert customer.stripe_customer_id is not None

    # Add a test card to the customer.
    if customer_profile.card:
        # Set the Stripe API key
        stripe.api_key = get_secret("stripe_secret_key")

        # Create a card payment method and attach it to the customer
        payment_method = stripe.PaymentMethod.create(
            type="card",
            card={"token": "tok_visa"},
        )

        # Attach the payment method to the customer
        stripe.PaymentMethod.attach(payment_method.id, customer=customer.stripe_customer_id)

        # Set the default payment method for the customer
        stripe.Customer.modify(
            customer.stripe_customer_id,
            invoice_settings={"default_payment_method": payment_method.id},
        )

    months = 12
    if customer_profile.billing_schedule == CustomerPlan.BILLING_SCHEDULE_MONTHLY:
        months = 1
    next_invoice_date = add_months(timezone_now(), months)

    customer_plan = CustomerPlan.objects.create(
        customer=customer,
        billing_cycle_anchor=timezone_now(),
        billing_schedule=customer_profile.billing_schedule,
        tier=customer_profile.tier,
        price_per_license=1200,
        automanage_licenses=customer_profile.automanage_licenses,
        status=customer_profile.status,
        charge_automatically=customer_profile.charge_automatically,
        next_invoice_date=next_invoice_date,
    )

    LicenseLedger.objects.create(
        licenses=10,
        licenses_at_next_renewal=10,
        event_time=timezone_now(),
        is_renewal=True,
        plan=customer_plan,
    )


def populate_remote_server(customer_profile: CustomerProfile) -> Dict[str, str]:
    unique_id = customer_profile.unique_id

    if customer_profile.tier == CustomerPlan.TIER_SELF_HOSTED_LEGACY:
        plan_type = RemoteZulipServer.PLAN_TYPE_SELF_HOSTED
    elif customer_profile.tier == CustomerPlan.TIER_SELF_HOSTED_BUSINESS:
        plan_type = RemoteZulipServer.PLAN_TYPE_BUSINESS
    else:
        raise AssertionError("Unexpected tier!")

    server_uuid = str(uuid.uuid4())
    api_key = server_uuid

    remote_server = RemoteZulipServer.objects.create(
        uuid=server_uuid,
        api_key=api_key,
        hostname=f"{unique_id}.example.com",
        contact_email=f"{unique_id}@example.com",
        plan_type=plan_type,
    )

    if customer_profile.tier == CustomerPlan.TIER_SELF_HOSTED_LEGACY:
        # Create customer plan for these servers for temporary period.
        billing_session = RemoteServerBillingSession(remote_server)
        renewal_date = datetime.strptime(customer_profile.renewal_date, TIMESTAMP_FORMAT).replace(
            tzinfo=timezone.utc
        )
        end_date = datetime.strptime(customer_profile.end_date, TIMESTAMP_FORMAT).replace(
            tzinfo=timezone.utc
        )
        billing_session.add_server_to_legacy_plan(renewal_date, end_date)
    elif customer_profile.tier == CustomerPlan.TIER_SELF_HOSTED_BUSINESS:
        # TBD
        pass

    if customer_profile.status == CustomerPlan.SWITCH_PLAN_TIER_AT_PLAN_END:
        # TBD
        pass

    return {
        "unique_id": unique_id,
        "server_uuid": server_uuid,
        "api_key": api_key,
    }
