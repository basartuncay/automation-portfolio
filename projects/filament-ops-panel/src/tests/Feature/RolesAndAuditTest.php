<?php

namespace Tests\Feature;

use App\Enums\InvoiceStatus;
use App\Enums\UserRole;
use App\Filament\Resources\Invoices\Pages\ListInvoices;
use App\Filament\Resources\Invoices\RelationManagers\EventsRelationManager;
use App\Models\Invoice;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use InvalidArgumentException;
use Laravel\Sanctum\Sanctum;
use Livewire\Livewire;
use Tests\TestCase;

class RolesAndAuditTest extends TestCase
{
    use RefreshDatabase;

    private function user(UserRole $role): User
    {
        $u = User::factory()->create(['role' => $role]);
        config(['app.panel_admin_emails' => User::pluck('email')->join(',')]);

        return $u;
    }

    public function test_viewer_sees_invoices_but_cannot_act(): void
    {
        $invoice = Invoice::factory()->needsReview()->create();
        $this->actingAs($this->user(UserRole::Viewer));
        Livewire::test(ListInvoices::class)
            ->assertCanSeeTableRecords([$invoice])
            ->assertTableActionHidden('approve', $invoice)
            ->assertTableActionHidden('reject', $invoice);
        $this->get('/admin/invoices/create')->assertForbidden();
    }

    public function test_payer_cannot_approve_and_approver_cannot_pay(): void
    {
        $pending = Invoice::factory()->create(['status' => InvoiceStatus::Pending]);
        $approved = Invoice::factory()->create(['status' => InvoiceStatus::Approved, 'approved_by' => null]);

        $this->actingAs($this->user(UserRole::Payer));
        Livewire::test(ListInvoices::class)
            ->assertTableActionHidden('approve', $pending)
            ->assertTableActionVisible('markPaid', $approved);

        $this->actingAs($this->user(UserRole::Approver));
        Livewire::test(ListInvoices::class)
            ->assertTableActionVisible('approve', $pending)
            ->assertTableActionHidden('markPaid', $approved);
    }

    public function test_four_eyes_the_approver_cannot_pay_their_own_approval(): void
    {
        $admin = $this->user(UserRole::Admin); // admins may do both steps, but never on the same invoice
        $this->actingAs($admin);
        $invoice = Invoice::factory()->create(['status' => InvoiceStatus::Pending]);
        Livewire::test(ListInvoices::class)->callTableAction('approve', $invoice);
        $invoice->refresh();

        Livewire::test(ListInvoices::class)->assertTableActionHidden('markPaid', $invoice);
        $this->expectException(InvalidArgumentException::class); // and the model refuses even without the UI
        $invoice->transitionTo(InvoiceStatus::Paid, $admin);
    }

    public function test_second_person_can_pay(): void
    {
        $approver = $this->user(UserRole::Approver);
        $payer = $this->user(UserRole::Payer);
        $invoice = Invoice::factory()->create(['status' => InvoiceStatus::Pending]);
        $this->actingAs($approver);
        $invoice->transitionTo(InvoiceStatus::Approved, $approver);

        $this->actingAs($payer);
        Livewire::test(ListInvoices::class)->callTableAction('markPaid', $invoice->refresh());
        $this->assertSame(InvoiceStatus::Paid, $invoice->refresh()->status);
    }

    public function test_audit_trail_records_who_did_what(): void
    {
        $approver = $this->user(UserRole::Approver);
        $this->actingAs($approver);
        $invoice = Invoice::factory()->create(['status' => InvoiceStatus::NeedsReview]);
        $invoice->update(['notes' => 'checked with vendor']);
        $invoice->transitionTo(InvoiceStatus::Approved, $approver);

        $events = $invoice->events()->get();
        $this->assertSame(['approved', 'edited', 'created'], $events->pluck('action')->all());
        $this->assertSame($approver->id, $events[0]->user_id);
        $this->assertSame(['from' => 'needs_review', 'to' => 'approved'], $events[0]->changes['status']);
        $this->assertSame(['from' => null, 'to' => 'checked with vendor'], $events[1]->changes['notes']);
        $this->assertContains('status: needs_review → approved', EventsRelationManager::describe($events[0]->changes));
    }

    public function test_api_ingestion_is_attributed_to_the_token_owner(): void
    {
        $bot = User::factory()->create(['name' => 'Intake bot']);
        Sanctum::actingAs($bot, ['invoices:write']);
        $this->postJson('/api/invoices', ['vendor' => 'V', 'number' => 'N1', 'currency' => 'USD', 'total' => 10, 'lines' => []])
            ->assertCreated();

        $this->assertSame($bot->id, Invoice::sole()->events()->first()->user_id);
    }

    public function test_only_admins_manage_users(): void
    {
        $this->actingAs($this->user(UserRole::Approver));
        $this->get('/admin/users')->assertForbidden();
        $this->actingAs($this->user(UserRole::Admin));
        $this->get('/admin/users')->assertOk();
    }
}
