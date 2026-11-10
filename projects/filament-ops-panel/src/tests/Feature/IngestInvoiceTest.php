<?php

namespace Tests\Feature;

use App\Enums\InvoiceStatus;
use App\Models\Invoice;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Laravel\Sanctum\Sanctum;
use Tests\TestCase;

class IngestInvoiceTest extends TestCase
{
    use RefreshDatabase;

    /** The JSON the invoice extractor's /extract endpoint returns, plus vendor-assigned fields. */
    private function payload(array $override = []): array
    {
        return array_merge([
            'vendor' => 'ACME Supplies Ltd', 'number' => 'INV-01042', 'date' => '2026-10-14', 'due_date' => '2026-11-13',
            'currency' => 'usd', 'subtotal' => 168.98, 'tax' => 33.8, 'total' => 202.78, 'file' => 'acme_1.pdf',
            'problems' => [],
            'lines' => [
                ['description' => 'Printer paper A4', 'qty' => 10, 'unit_price' => 4.5, 'amount' => 45.0],
                ['description' => 'Toner cartridge', 'qty' => 2, 'unit_price' => 61.99, 'amount' => 123.98],
            ],
        ], $override);
    }

    private function asService(array $abilities = ['invoices:write']): void
    {
        Sanctum::actingAs(User::factory()->create(), $abilities);
    }

    public function test_requires_a_token(): void
    {
        $this->postJson('/api/invoices', $this->payload())->assertUnauthorized();
    }

    public function test_token_needs_the_write_ability(): void
    {
        $this->asService(['something:else']);
        $this->postJson('/api/invoices', $this->payload())->assertForbidden();
    }

    public function test_creates_vendor_invoice_and_lines(): void
    {
        $this->asService();
        $this->postJson('/api/invoices', $this->payload())->assertCreated()->assertJson(['status' => 'pending', 'updated' => false]);

        $invoice = Invoice::with('vendor', 'lines')->sole();
        $this->assertSame('ACME Supplies Ltd', $invoice->vendor->name);
        $this->assertSame('USD', $invoice->currency);
        $this->assertSame('202.78', $invoice->total);
        $this->assertCount(2, $invoice->lines);
    }

    public function test_problems_route_to_review(): void
    {
        $this->asService();
        $this->postJson('/api/invoices', $this->payload(['problems' => ['subtotal + tax != total']]))
            ->assertCreated()->assertJson(['status' => 'needs_review']);
        $this->assertSame(['subtotal + tax != total'], Invoice::sole()->problems);
    }

    public function test_resending_updates_instead_of_duplicating(): void
    {
        $this->asService();
        $this->postJson('/api/invoices', $this->payload(['problems' => ['missing date']]))->assertCreated();
        $this->postJson('/api/invoices', $this->payload(['lines' => [['description' => 'Fixed', 'qty' => 1, 'unit_price' => 1, 'amount' => 1]]]))
            ->assertOk()->assertJson(['updated' => true, 'status' => 'pending']);

        $invoice = Invoice::sole();
        $this->assertNull($invoice->problems);
        $this->assertSame(['Fixed'], $invoice->lines()->pluck('description')->all());
    }

    public function test_never_overwrites_an_approved_invoice(): void
    {
        $this->asService();
        $this->postJson('/api/invoices', $this->payload())->assertCreated();
        Invoice::sole()->transitionTo(InvoiceStatus::Approved);

        $this->postJson('/api/invoices', $this->payload(['total' => 999]))->assertStatus(409);
        $this->assertSame('202.78', Invoice::sole()->total);
    }

    public function test_validates_payload(): void
    {
        $this->asService();
        $this->postJson('/api/invoices', $this->payload(['total' => -5, 'currency' => 'dollars', 'lines' => [['qty' => 1]]]))
            ->assertUnprocessable()
            ->assertJsonValidationErrors(['total', 'currency', 'lines.0.description', 'lines.0.amount']);
    }

    public function test_known_token_can_be_registered_idempotently_for_compose(): void
    {
        $bot = User::factory()->create(['email' => 'intake@example.com']);
        $secret = str_repeat('k', 40);
        $this->artisan('invoices:token', ['email' => 'intake@example.com', '--token' => $secret])->assertSuccessful();
        $this->artisan('invoices:token', ['email' => 'intake@example.com', '--token' => $secret])->assertSuccessful();
        $this->assertSame(1, $bot->tokens()->count());
        $this->artisan('invoices:token', ['email' => 'intake@example.com', '--token' => 'short'])->assertFailed();

        $this->postJson('/api/invoices', $this->payload(), ['Authorization' => "Bearer {$secret}"])->assertCreated();
    }
}
