<?php

namespace App\Filament\Widgets;

use App\Enums\InvoiceStatus;
use App\Models\Invoice;
use Filament\Widgets\StatsOverviewWidget;
use Filament\Widgets\StatsOverviewWidget\Stat;
use Illuminate\Database\Eloquent\Builder;
use Illuminate\Support\Number;

class InvoiceStats extends StatsOverviewWidget
{
    protected static ?int $sort = 1;

    /** "USD 1,200.00 · EUR 300.00": totals are never added across currencies. */
    public static function totalsByCurrency(Builder $query): string
    {
        $rows = $query->selectRaw('currency, sum(total) as amount')->groupBy('currency')->orderBy('currency')->get();

        return $rows->isEmpty() ? '0' : $rows->map(fn ($r) => Number::currency((float) $r->amount, $r->currency))->join(' · ');
    }

    protected function getStats(): array
    {
        $review = Invoice::where('status', InvoiceStatus::NeedsReview)->count();
        $overdue = Invoice::query()->overdue();
        $dueSoon = Invoice::where('status', InvoiceStatus::Approved)->whereBetween('due_date', [now()->startOfDay(), now()->addDays(7)]);
        $paidMonth = Invoice::where('status', InvoiceStatus::Paid)->where('paid_at', '>=', now()->startOfMonth());

        return [
            Stat::make('Needs review', $review)
                ->description($review ? 'Numbers did not add up or vendor unknown' : 'Nothing waiting')
                ->color($review ? 'warning' : 'success'),
            Stat::make('Overdue', (clone $overdue)->count())
                ->description(self::totalsByCurrency(clone $overdue))
                ->color('danger'),
            Stat::make('Approved, due in 7 days', (clone $dueSoon)->count())
                ->description(self::totalsByCurrency(clone $dueSoon)),
            Stat::make('Paid this month', (clone $paidMonth)->count())
                ->description(self::totalsByCurrency(clone $paidMonth))
                ->color('success'),
        ];
    }
}
