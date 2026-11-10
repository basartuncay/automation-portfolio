<?php

namespace Tests\Feature;

use App\Enums\InvoiceStatus;
use App\Filament\Resources\Invoices\Pages\ListInvoices;
use App\Filament\Resources\Invoices\Pages\ViewInvoice;
use App\Filament\Widgets\InvoiceStats;
use App\Models\Invoice;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use InvalidArgumentException;
use Livewire\Livewire;
use Tests\TestCase;

class InvoicePanelTest extends TestCase
{
    use RefreshDatabase;

    private User $admin;

    protected function setUp(): void
    {
        parent::setUp();
        $this->admin = User::factory()->create(['role' => \App\Enums\UserRole::Admin]);
        config(['app.panel_admin_emails' => $this->admin->email]); // tests run outside "local", so access is enforced
        $this->actingAs($this->admin);
    }

    public function test_status_rules(): void
    {
        $this->assertTrue(InvoiceStatus::NeedsReview->canBecome(InvoiceStatus::Approved));
        $this->assertFalse(InvoiceStatus::Pending->canBecome(InvoiceStatus::Paid)); // must be approved first
        $this->assertFalse(InvoiceStatus::Paid->canBecome(InvoiceStatus::Rejected));

        $this->expectException(InvalidArgumentException::class);
        Invoice::factory()->create(['status' => InvoiceStatus::Paid])->transitionTo(InvoiceStatus::Approved);
    }

    public function test_dashboard_and_pages_render(): void
    {
        $invoice = Invoice::factory()->needsReview()->create();
        $this->get('/admin')->assertOk();
        $this->get('/admin/invoices')->assertOk()->assertSee($invoice->number);
        Livewire::test(ViewInvoice::class, ['record' => $invoice->getRouteKey()])->assertOk()->assertSee('Why it needs review');
    }

    public function test_approve_records_who_and_when(): void
    {
        $invoice = Invoice::factory()->needsReview()->create();
        Livewire::test(ListInvoices::class)->callTableAction('approve', $invoice);

        $invoice->refresh();
        $this->assertSame(InvoiceStatus::Approved, $invoice->status);
        $this->assertSame($this->admin->id, $invoice->approved_by);
        $this->assertNotNull($invoice->approved_at);
    }

    public function test_actions_only_shown_for_allowed_transitions(): void
    {
        $paid = Invoice::factory()->create(['status' => InvoiceStatus::Paid]);
        $pending = Invoice::factory()->create(['status' => InvoiceStatus::Pending]);
        Livewire::test(ListInvoices::class)
            ->assertTableActionHidden('approve', $paid)
            ->assertTableActionHidden('reject', $paid)
            ->assertTableActionHidden('markPaid', $pending)
            ->assertTableActionVisible('approve', $pending);
    }

    public function test_reject_requires_a_reason(): void
    {
        $invoice = Invoice::factory()->create(['status' => InvoiceStatus::Pending]);
        Livewire::test(ListInvoices::class)
            ->callTableAction('reject', $invoice, data: ['notes' => ''])
            ->assertHasTableActionErrors(['notes' => 'required']);
        Livewire::test(ListInvoices::class)->callTableAction('reject', $invoice, data: ['notes' => 'Duplicate of INV-1']);

        $this->assertSame(InvoiceStatus::Rejected, $invoice->refresh()->status);
        $this->assertSame('Duplicate of INV-1', $invoice->notes);
    }

    public function test_bulk_approve_skips_final_invoices(): void
    {
        $review = Invoice::factory()->needsReview()->create();
        $paid = Invoice::factory()->create(['status' => InvoiceStatus::Paid]);
        Livewire::test(ListInvoices::class)->callTableBulkAction('approveSelected', [$review, $paid]);

        $this->assertSame(InvoiceStatus::Approved, $review->refresh()->status);
        $this->assertSame(InvoiceStatus::Paid, $paid->refresh()->status);
    }

    public function test_overdue_filter(): void
    {
        $late = Invoice::factory()->create(['status' => InvoiceStatus::Approved, 'due_date' => now()->subDays(3)]);
        $paidLate = Invoice::factory()->create(['status' => InvoiceStatus::Paid, 'due_date' => now()->subDays(3)]);
        $future = Invoice::factory()->create(['status' => InvoiceStatus::Approved, 'due_date' => now()->addDays(10)]);
        Livewire::test(ListInvoices::class)
            ->filterTable('overdue', true)
            ->assertCanSeeTableRecords([$late])
            ->assertCanNotSeeTableRecords([$paidLate, $future]);
    }

    public function test_totals_are_not_mixed_across_currencies(): void
    {
        Invoice::factory()->create(['status' => InvoiceStatus::Approved, 'due_date' => now()->subDay(), 'currency' => 'USD', 'total' => 100]);
        Invoice::factory()->create(['status' => InvoiceStatus::Approved, 'due_date' => now()->subDay(), 'currency' => 'EUR', 'total' => 50]);
        $this->assertSame('€50.00 · $100.00', InvoiceStats::totalsByCurrency(Invoice::query()->overdue()));
    }

    public function test_panel_locked_to_listed_admins_outside_local(): void
    {
        $this->app['env'] = 'production';
        config(['app.panel_admin_emails' => 'boss@example.com']);
        $this->get('/admin')->assertForbidden();
        $this->actingAs(User::factory()->create(['email' => 'Boss@example.com']))->get('/admin')->assertOk();
    }
}
