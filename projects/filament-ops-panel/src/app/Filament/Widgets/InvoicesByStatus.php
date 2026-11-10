<?php

namespace App\Filament\Widgets;

use App\Enums\InvoiceStatus;
use App\Models\Invoice;
use Filament\Widgets\ChartWidget;

class InvoicesByStatus extends ChartWidget
{
    protected static ?int $sort = 2;

    protected ?string $heading = 'Invoices by status';

    protected ?string $maxHeight = '260px';

    protected function getData(): array
    {
        $counts = Invoice::query()->selectRaw('status, count(*) as n')->groupBy('status')->pluck('n', 'status');
        $palette = ['gray' => '#9ca3af', 'warning' => '#f59e0b', 'info' => '#3b82f6', 'success' => '#10b981', 'danger' => '#ef4444'];
        $cases = InvoiceStatus::cases();

        return [
            'datasets' => [[
                'data' => array_map(fn ($s) => (int) ($counts[$s->value] ?? 0), $cases),
                'backgroundColor' => array_map(fn ($s) => $palette[$s->getColor()], $cases),
            ]],
            'labels' => array_map(fn ($s) => $s->getLabel(), $cases),
        ];
    }

    protected function getType(): string
    {
        return 'doughnut';
    }
}
