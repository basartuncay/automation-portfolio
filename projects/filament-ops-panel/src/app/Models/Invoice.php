<?php

namespace App\Models;

use App\Enums\InvoiceStatus;
use Illuminate\Database\Eloquent\Builder;
use Illuminate\Database\Eloquent\Factories\HasFactory;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;
use Illuminate\Database\Eloquent\Relations\HasMany;
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
