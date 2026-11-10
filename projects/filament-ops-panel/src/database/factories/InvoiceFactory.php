<?php

namespace Database\Factories;

use App\Enums\InvoiceStatus;
use App\Models\Invoice;
use App\Models\User;
use App\Models\Vendor;
use Illuminate\Database\Eloquent\Factories\Factory;

class InvoiceFactory extends Factory
{
    public const ITEMS = [
        'Printer paper A4 (box)' => 24.90, 'Toner cartridge' => 61.99, 'Office chair' => 189.00, 'Monitor 27"' => 279.00,
        'Cloud hosting (monthly)' => 249.00, 'Laptop stand' => 39.50, 'Cleaning service' => 420.00, 'Coffee beans 5kg' => 96.00,
        'Software licence' => 899.00, 'Courier delivery' => 18.75, 'Network switch' => 349.00, 'Security audit' => 2400.00,
    ];

    public function definition(): array
    {
        $date = fake()->dateTimeBetween('-50 days', '-1 day');

        return [
            'vendor_id' => Vendor::factory(),
            'number' => 'INV-'.fake()->unique()->numerify('#####'),
            'invoice_date' => $date,
            'due_date' => (clone $date)->modify('+30 days'),
            'currency' => 'USD',
            'subtotal' => 0,
            'tax' => 0,
            'total' => 0,
            'status' => fake()->randomElement([
                InvoiceStatus::Paid, InvoiceStatus::Paid, InvoiceStatus::Paid, InvoiceStatus::Approved,
                InvoiceStatus::Approved, InvoiceStatus::Pending, InvoiceStatus::Pending, InvoiceStatus::Rejected,
            ]),
            'problems' => null,
        ];
    }

    /** Lines, totals and approval fields that agree with the status, so the demo data looks like real data. */
    public function configure(): static
    {
        return $this->afterCreating(function (Invoice $invoice) {
            if ($invoice->lines()->exists() || (float) $invoice->total > 0) {
                return; // caller supplied its own amounts
            }
            $subtotal = 0;
            foreach (fake()->randomElements(array_keys(self::ITEMS), fake()->numberBetween(1, 3)) as $item) {
                $qty = fake()->numberBetween(1, 6);
                $amount = round(self::ITEMS[$item] * $qty, 2);
                $invoice->lines()->create(['description' => $item, 'qty' => $qty, 'unit_price' => self::ITEMS[$item], 'amount' => $amount]);
                $subtotal += $amount;
            }
            $tax = round($subtotal * 0.2, 2);
            $invoice->forceFill(['subtotal' => $subtotal, 'tax' => $tax, 'total' => $subtotal + $tax]);

            if (in_array($invoice->status, [InvoiceStatus::Approved, InvoiceStatus::Paid], true)) {
                $approvedAt = $invoice->invoice_date->copy()->addDays(fake()->numberBetween(1, 5));
                $invoice->forceFill(['approved_by' => User::query()->inRandomOrder()->value('id'), 'approved_at' => $approvedAt]);
                if ($invoice->status === InvoiceStatus::Paid) {
                    $invoice->paid_at = min($approvedAt->copy()->addDays(fake()->numberBetween(2, 20)), now());
                }
            }
            if ($invoice->status === InvoiceStatus::Rejected) {
                $invoice->notes = fake()->randomElement(['Duplicate invoice', 'Not ordered by us', 'Wrong VAT number']);
            }
            $invoice->save();
        });
    }

    public function needsReview(): static
    {
        return $this->state(fn () => [
            'status' => InvoiceStatus::NeedsReview,
            'problems' => [fake()->randomElement(['subtotal + tax != total', 'line items do not sum to subtotal', 'missing due date'])],
        ]);
    }
}
