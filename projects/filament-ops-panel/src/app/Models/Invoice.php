<?php

namespace App\Models;

use App\Enums\InvoiceStatus;
use Illuminate\Database\Eloquent\Builder;
use Illuminate\Database\Eloquent\Factories\HasFactory;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;
use Illuminate\Database\Eloquent\Relations\HasMany;
use Illuminate\Support\Arr;
use InvalidArgumentException;

class Invoice extends Model
{
    use HasFactory;

    protected $fillable = [
        'vendor_id', 'number', 'invoice_date', 'due_date', 'currency', 'subtotal', 'tax', 'total',
        'status', 'problems', 'source_file', 'notes',
    ];

    protected function casts(): array
    {
        return [
            'invoice_date' => 'date',
            'due_date' => 'date',
            'subtotal' => 'decimal:2',
            'tax' => 'decimal:2',
            'total' => 'decimal:2',
            'status' => InvoiceStatus::class,
            'problems' => 'array',
            'approved_at' => 'datetime',
            'paid_at' => 'datetime',
        ];
    }

    /** Every create and change is written to invoice_events with the acting user (panel user or API token owner). */
    protected static function booted(): void
    {
        static::created(fn (Invoice $invoice) => $invoice->log('created', Arr::only($invoice->getAttributes(), ['number', 'total', 'currency', 'status'])));

        static::updated(function (Invoice $invoice) {
            $changes = Arr::except($invoice->getChanges(), ['updated_at']);
            if (! $changes) {
                return;
            }
            $diff = [];
            foreach ($changes as $field => $new) {
                $diff[$field] = ['from' => $invoice->getOriginal($field) instanceof \BackedEnum ? $invoice->getOriginal($field)->value : $invoice->getOriginal($field), 'to' => $new];
            }
            $action = isset($changes['status']) ? (string) $invoice->status->value : 'edited';
            $invoice->log($action, $diff);
        });
    }

    public function log(string $action, ?array $changes = null): void
    {
        $this->events()->create(['action' => $action, 'changes' => $changes, 'user_id' => auth()->id()]);
    }

    public function events(): HasMany
    {
        return $this->hasMany(InvoiceEvent::class)->latest('id');
    }

    public function vendor(): BelongsTo
    {
        return $this->belongsTo(Vendor::class);
    }

    public function lines(): HasMany
    {
        return $this->hasMany(InvoiceLine::class);
    }

    public function approver(): BelongsTo
    {
        return $this->belongsTo(User::class, 'approved_by');
    }

    public function scopeOverdue(Builder $q): Builder
    {
        return $q->whereIn('status', [InvoiceStatus::Pending, InvoiceStatus::NeedsReview, InvoiceStatus::Approved])
            ->whereDate('due_date', '<', now());
    }

    public function isOverdue(): bool
    {
        return $this->due_date?->isPast() && ! in_array($this->status, [InvoiceStatus::Paid, InvoiceStatus::Rejected], true);
    }

    public function transitionTo(InvoiceStatus $next, ?User $by = null): void
    {
        if (! $this->status->canBecome($next)) {
            throw new InvalidArgumentException("Cannot move invoice {$this->number} from {$this->status->value} to {$next->value}");
        }
        if ($next === InvoiceStatus::Paid && $by && $this->approved_by === $by->id) {
            // Same rule as InvoicePolicy::pay, enforced here too so no code path can skip it.
            throw new InvalidArgumentException("The approver of invoice {$this->number} cannot also mark it paid");
        }
        $this->status = $next;
        if ($next === InvoiceStatus::Approved) {
            $this->approved_by = $by?->id;
            $this->approved_at = now();
        }
        if ($next === InvoiceStatus::Paid) {
            $this->paid_at = now();
        }
        $this->save();
    }
}
