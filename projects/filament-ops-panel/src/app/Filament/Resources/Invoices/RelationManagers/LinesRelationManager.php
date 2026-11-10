<?php

namespace App\Filament\Resources\Invoices\RelationManagers;

use Filament\Actions\BulkActionGroup;
use Filament\Actions\CreateAction;
use Filament\Actions\DeleteAction;
use Filament\Actions\DeleteBulkAction;
use Filament\Actions\EditAction;
use Filament\Forms\Components\TextInput;
use Filament\Resources\RelationManagers\RelationManager;
use Filament\Schemas\Components\Utilities\Get;
use Filament\Schemas\Components\Utilities\Set;
use Filament\Schemas\Schema;
use Filament\Tables\Columns\Summarizers\Sum;
use Filament\Tables\Columns\TextColumn;
use Filament\Tables\Table;

class LinesRelationManager extends RelationManager
{
    protected static string $relationship = 'lines';

    protected static ?string $title = 'Line items';

    public function form(Schema $schema): Schema
    {
        $recalc = fn (Get $get, Set $set) => $set('amount', round((float) $get('qty') * (float) $get('unit_price'), 2));

        return $schema
            ->components([
                TextInput::make('description')->required()->maxLength(255)->columnSpanFull(),
                TextInput::make('qty')->numeric()->integer()->minValue(1)->default(1)->required()->live(onBlur: true)
                    ->afterStateUpdated($recalc),
                TextInput::make('unit_price')->numeric()->minValue(0)->required()->live(onBlur: true)
                    ->afterStateUpdated($recalc),
                TextInput::make('amount')->numeric()->required()->readOnly()
                    ->helperText('qty × unit price'),
            ])->columns(3);
    }

    public function table(Table $table): Table
    {
        $currency = fn () => $this->getOwnerRecord()->currency;

        return $table
            ->recordTitleAttribute('description')
            ->columns([
                TextColumn::make('description')->searchable(),
                TextColumn::make('qty')->alignEnd(),
                TextColumn::make('unit_price')->money($currency)->alignEnd(),
                TextColumn::make('amount')->money($currency)->alignEnd()
                    ->summarize(Sum::make()->money($currency)->label('Lines total')),
            ])
            ->headerActions([CreateAction::make()])
            ->recordActions([EditAction::make(), DeleteAction::make()])
            ->toolbarActions([BulkActionGroup::make([DeleteBulkAction::make()])]);
    }
}
