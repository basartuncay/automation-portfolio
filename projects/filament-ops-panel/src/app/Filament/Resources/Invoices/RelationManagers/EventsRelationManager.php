<?php

namespace App\Filament\Resources\Invoices\RelationManagers;

use App\Models\InvoiceEvent;
use Filament\Resources\RelationManagers\RelationManager;
use Filament\Tables\Columns\TextColumn;
use Filament\Tables\Table;

/** Read-only audit trail: no create, edit or delete actions exist on purpose. */
class EventsRelationManager extends RelationManager
{
    protected static string $relationship = 'events';

    protected static ?string $title = 'History';

    public function isReadOnly(): bool
    {
        return true;
    }

    public function table(Table $table): Table
    {
        return $table
            ->defaultSort('id', 'desc')
            ->paginated([10, 25])
            ->columns([
                TextColumn::make('created_at')->label('When')->dateTime()->sortable(),
                TextColumn::make('user.name')->label('Who')->placeholder('system'),
                TextColumn::make('action')->badge()->color(fn (string $state) => match ($state) {
                    'approved' => 'info', 'paid' => 'success', 'rejected' => 'danger', 'needs_review' => 'warning', default => 'gray',
                }),
                TextColumn::make('changes')
                    ->label('What changed')
                    ->state(fn (InvoiceEvent $record) => self::describe($record->changes ?? []))
                    ->listWithLineBreaks()
                    ->wrap(),
            ]);
    }

    /** ["status: pending → approved", "total: 100.00 → 120.00"] */
    public static function describe(array $changes): array
    {
        $show = fn ($v) => is_array($v) ? json_encode($v) : (string) ($v ?? '—');

        return collect($changes)->map(fn ($v, $field) => is_array($v) && array_key_exists('to', $v)
            ? "{$field}: {$show($v['from'])} → {$show($v['to'])}"
            : "{$field}: {$show($v)}")->values()->all();
    }
}
