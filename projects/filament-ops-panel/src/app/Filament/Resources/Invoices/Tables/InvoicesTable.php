<?php

namespace App\Filament\Resources\Invoices\Tables;

use App\Enums\InvoiceStatus;
use App\Models\Invoice;
use Filament\Actions\Action;
use Filament\Actions\ActionGroup;
use Filament\Actions\BulkAction;
use Filament\Actions\BulkActionGroup;
use Filament\Actions\EditAction;
use Filament\Actions\ViewAction;
use Filament\Forms\Components\Textarea;
use Filament\Notifications\Notification;
use Filament\Tables\Columns\TextColumn;
use Filament\Tables\Filters\Filter;
use Filament\Tables\Filters\SelectFilter;
use Filament\Tables\Table;
use Illuminate\Database\Eloquent\Builder;
use Illuminate\Database\Eloquent\Collection;

class InvoicesTable
{
    public static function configure(Table $table): Table
    {
        return $table
            ->defaultSort('due_date')
            ->columns([
                TextColumn::make('vendor.name')->searchable()->sortable(),
                TextColumn::make('number')->searchable()->copyable(),
                TextColumn::make('total')
                    ->money(fn (Invoice $record) => $record->currency)
                    ->sortable()
                    ->alignEnd(),
                TextColumn::make('status')->badge()->sortable(),
                TextColumn::make('due_date')
                    ->date()
                    ->sortable()
                    ->color(fn (Invoice $record) => $record->isOverdue() ? 'danger' : null)
                    ->description(fn (Invoice $record) => $record->isOverdue() ? 'Overdue' : null),
                TextColumn::make('problems')
                    ->label('Review reason')
                    ->listWithLineBreaks()
                    ->placeholder('—')
                    ->color('warning')
                    ->toggleable(),
                TextColumn::make('approver.name')->label('Approved by')->toggleable(isToggledHiddenByDefault: true),
                TextColumn::make('invoice_date')->date()->sortable()->toggleable(isToggledHiddenByDefault: true),
                TextColumn::make('source_file')->toggleable(isToggledHiddenByDefault: true),
            ])
            ->filters([
                SelectFilter::make('status')->options(InvoiceStatus::class)->multiple(),
                SelectFilter::make('vendor')->relationship('vendor', 'name')->searchable()->preload(),
                Filter::make('overdue')
                    ->label('Overdue only')
                    ->toggle()
                    ->query(fn (Builder $query) => $query->overdue()),
            ])
            ->recordActions([
                self::transition('approve', 'Approve', InvoiceStatus::Approved, 'heroicon-o-check', 'info'),
                self::transition('markPaid', 'Mark paid', InvoiceStatus::Paid, 'heroicon-o-banknotes', 'success'),
                self::transition('reject', 'Reject', InvoiceStatus::Rejected, 'heroicon-o-x-mark', 'danger')
                    ->schema([Textarea::make('notes')->label('Reason')->required()])
                    ->action(function (Invoice $record, array $data) {
                        $record->notes = $data['notes'];
                        $record->transitionTo(InvoiceStatus::Rejected, auth()->user());
                        Notification::make()->title("Invoice {$record->number} rejected")->success()->send();
                    }),
                ActionGroup::make([ViewAction::make(), EditAction::make()]),
            ])
            ->toolbarActions([
                BulkActionGroup::make([
                    BulkAction::make('approveSelected')
                        ->label('Approve selected')
                        ->icon('heroicon-o-check')
                        ->requiresConfirmation()
                        ->action(function (Collection $records) {
                            $ok = $records->filter(fn (Invoice $r) => $r->status->canBecome(InvoiceStatus::Approved));
                            $ok->each(fn (Invoice $r) => $r->transitionTo(InvoiceStatus::Approved, auth()->user()));
                            $skipped = $records->count() - $ok->count();
                            Notification::make()
                                ->title("{$ok->count()} approved".($skipped ? ", {$skipped} skipped (already paid, approved or rejected)" : ''))
                                ->success()->send();
                        })
                        ->deselectRecordsAfterCompletion(),
                ]),
            ]);
    }

    private static function transition(string $name, string $label, InvoiceStatus $to, string $icon, string $color): Action
    {
        return Action::make($name)
            ->label($label)
            ->icon($icon)
            ->color($color)
            ->requiresConfirmation()
            ->visible(fn (Invoice $record) => $record->status->canBecome($to))
            ->action(function (Invoice $record) use ($to, $label) {
                $record->transitionTo($to, auth()->user());
                Notification::make()->title("{$label}: invoice {$record->number}")->success()->send();
            });
    }
}
